from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

from app.config import get_settings
from app.database import db_session
from app.services.document_core import fetch_document_pdf, fetch_document_refs, search_document_roots
from app.services.browser_identity import normalize_client_id
from app.services.pdf_assembler import assemble_pdf
from app.services.reconciliation import (
    fetch_kbank_deposit_rows,
    fetch_uob_transactions,
    find_rv_kbank_candidates,
    find_rv_uob_candidates,
)
from app.services.receive_voucher_extractor import normalize_ref
from app.services.receive_voucher_extractor import extract_bank_rows
from app.services.receive_voucher_importer import import_receive_voucher_pdf
from app.services.storage import read_pdf_from_storage
from app.services.upload_jobs import (
    UploadCancelled,
    create_upload_job,
    get_upload_job,
    is_upload_job_cancel_requested,
    list_active_upload_jobs,
    list_upload_job_history,
    mark_upload_job_cancelled,
    mark_upload_job_done,
    mark_upload_job_failed,
    mark_upload_job_processing,
    request_upload_job_cancel,
    update_upload_job_progress,
)
from app.services.upload_worker import enqueue_upload_task, request_upload_task_cancel


router = APIRouter(prefix="/api/receive-vouchers", tags=["receive-vouchers"])

ALLOWED_REF_TYPES = {
    "invoice_no",
    "bank_code",
    "bank_account_no",
    "check_no",
    "bill_no",
    "customer_code",
    "journal_name",
}

VOUCHER_DATE_DISPLAY_SQL = (
    "CASE "
    "WHEN rv.voucher_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
    "THEN substr(rv.voucher_date, 9, 2) || '/' || substr(rv.voucher_date, 6, 2) || '/' || substr(rv.voucher_date, 1, 4) "
    "ELSE rv.voucher_date "
    "END"
)

REF_TYPE_ORDER_SQL = (
    "CASE ref_type "
    "WHEN 'bank_account_no' THEN 10 "
    "WHEN 'bank_code' THEN 20 "
    "WHEN 'check_no' THEN 30 "
    "WHEN 'customer_code' THEN 40 "
    "WHEN 'invoice_no' THEN 50 "
    "WHEN 'bill_no' THEN 60 "
    "WHEN 'journal_name' THEN 70 "
    "ELSE 999 END"
)


def normalize_date_filter(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise HTTPException(
        status_code=422,
        detail="Date filters must use YYYY-MM-DD or DD/MM/YYYY",
    )


def normalize_check_no_filter(value: str) -> str:
    normalized = re.sub(r"\s+", "", value.strip().upper())
    normalized = re.sub(r"#\d+$", "", normalized)
    normalized = re.sub(r"-\d+$", "", normalized)
    return normalized


def validate_ref_filter(ref_type: str | None, ref_value: str | None) -> None:
    if bool(ref_type) != bool(ref_value):
        raise HTTPException(status_code=422, detail="ref_type and ref_value must be provided together")
    if ref_type and ref_type not in ALLOWED_REF_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported ref_type: {ref_type}")


def _process_upload_job(job_id: str, temp_path: Path) -> None:
    try:
        pdf_bytes = temp_path.read_bytes()
        total_pages = len(PdfReader(BytesIO(pdf_bytes)).pages)

        with db_session() as conn:
            mark_upload_job_processing(conn, job_id, total_pages)

        last_progress_page = 0

        def on_page(page_number: int) -> None:
            with db_session() as conn:
                if is_upload_job_cancel_requested(conn, job_id):
                    raise UploadCancelled()
                nonlocal last_progress_page
                should_flush = (
                    page_number == total_pages
                    or page_number - last_progress_page >= 8
                    or last_progress_page == 0
                )
                if should_flush:
                    update_upload_job_progress(conn, job_id, page_number)
                    last_progress_page = page_number

        with db_session() as conn:
            result = import_receive_voucher_pdf(conn, pdf_bytes, on_page=on_page)
            mark_upload_job_done(conn, job_id, result)
    except UploadCancelled:
        with db_session() as conn:
            mark_upload_job_cancelled(conn, job_id)
    except Exception as exc:
        with db_session() as conn:
            mark_upload_job_failed(conn, job_id, str(exc))
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/upload", status_code=202)
async def upload_receive_vouchers(
    file: Annotated[UploadFile, File(...)],
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    settings = get_settings()
    job_id = str(uuid4())

    uploads_tmp_dir = settings.resolved_app_data_dir / "uploads_tmp"
    uploads_tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = (uploads_tmp_dir / f"{job_id}.pdf").resolve()

    pdf_bytes = await file.read()
    temp_path.write_bytes(pdf_bytes)
    client_id = normalize_client_id(x_client_id)

    with db_session() as conn:
        create_upload_job(conn, job_id, file.filename, client_id, "receive-vouchers")

    enqueue_upload_task(
        "receive-vouchers",
        job_id,
        temp_path,
        _process_upload_job,
        provenance={
            "client_id": client_id,
            "source_page": "/receive-vouchers",
            "source_action": "upload",
            "source_file_name": file.filename,
        },
    )

    return {"status": "pending", "job_id": job_id, "filename": file.filename}


def _client_id_from_header(x_client_id: str | None) -> str:
    return normalize_client_id(x_client_id)


@router.get("/upload/active")
def get_active_upload_jobs_status(
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
):
    with db_session() as conn:
        return list_active_upload_jobs(conn)


@router.get("/upload/history")
def get_upload_job_history(
    limit: int = 20,
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
):
    with db_session() as conn:
        return list_upload_job_history(conn, limit=limit)


@router.post("/upload/{job_id}/cancel")
def cancel_upload_job(
    job_id: str,
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
):
    client_id = _client_id_from_header(x_client_id)
    with db_session() as conn:
        job = get_upload_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Upload job not found")
        if job.get("client_id") not in ("", client_id):
            raise HTTPException(status_code=403, detail="Upload job belongs to another browser")
        if job["status"] not in ("pending", "processing"):
            raise HTTPException(status_code=409, detail="Upload job already finished")
        request_upload_job_cancel(conn, job_id, client_id)
    request_upload_task_cancel("receive-vouchers", job_id)

    return {"status": "cancel_requested"}


@router.get("/upload/{job_id}")
def get_upload_job_status(
    job_id: str,
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
):
    with db_session() as conn:
        job = get_upload_job(conn, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Upload job not found")

    return job


@router.get("")
def search_receive_vouchers(
    voucher_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    validate_ref_filter(ref_type, ref_value)
    with db_session() as conn:
        normalized_ref_value = normalize_ref(ref_value) if ref_value else None
        if ref_type == "check_no" and ref_value:
            normalized_ref_value = normalize_check_no_filter(ref_value)
        rows = [dict(row) for row in search_document_roots(
            conn,
            doc_type="receive-vouchers",
            root_key=normalize_ref(voucher_number) if voucher_number else None,
            root_date_from=normalize_date_filter(date_from) if date_from else None,
            root_date_to=normalize_date_filter(date_to) if date_to else None,
            ref_type=ref_type,
            ref_value=normalized_ref_value,
        )]
        # Fetch once and reuse across every row below. Otherwise the UOB/KBank
        # candidate lookups rescan their statement-row tables per voucher.
        uob_transactions = fetch_uob_transactions(conn)
        kbank_deposit_rows_by_check = {}
        kbank_deposit_rows_by_amount = {}
        for kbank_row in fetch_kbank_deposit_rows(conn):
            kbank_deposit_rows_by_check.setdefault(str(kbank_row["normalized_check_no"]), []).append(kbank_row)
            amount_key = str(kbank_row.get("amount") or "")
            if amount_key:
                kbank_deposit_rows_by_amount.setdefault(amount_key, []).append(kbank_row)
        for row in rows:
            row["voucher_number"] = row.pop("root_key")
            iso = row.get("root_date")
            row["voucher_date_iso"] = iso
            row["voucher_date"] = (
                f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) == 10 else iso
            )
            refs = fetch_document_refs(conn, row["id"]) if row.get("id") else {}
            row["refs"] = {rt: ", ".join(vs) for rt, vs in refs.items()}
            stored_bank_rows = [
                dict(bank_row)
                for bank_row in conn.execute(
                    """
                    SELECT id, row_order, bank_code, bank_account_no, check_no, bill_no, currency_amt_raw, currency_amt
                    FROM receive_voucher_bank_rows
                    WHERE document_root_id = ?
                    ORDER BY row_order
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            bank_rows = stored_bank_rows
            if row.get("id"):
                raw_text_rows = conn.execute(
                    """
                    SELECT raw_text
                    FROM document_pages
                    WHERE document_root_id = ?
                    ORDER BY page_order
                    """,
                    (row["id"],),
                ).fetchall()
                extracted_bank_rows: list[dict[str, str]] = []
                saw_raw_text = False
                for raw_text_row in raw_text_rows:
                    raw_text = str(raw_text_row["raw_text"] or "")
                    if not raw_text:
                        continue
                    saw_raw_text = True
                    for bank_row in extract_bank_rows(raw_text, row["voucher_number"]):
                        if bank_row not in extracted_bank_rows:
                            extracted_bank_rows.append(bank_row)
                if saw_raw_text:
                    for extracted_index, extracted_row in enumerate(extracted_bank_rows, start=1):
                        extracted_row.setdefault("row_order", extracted_index)
                    bank_rows = extracted_bank_rows
            matches_by_row_order = {
                item["rv_bank_row"].get("row_order"): item
                for item in find_rv_uob_candidates(
                    conn, voucher_number=row["voucher_number"], uob_transactions=uob_transactions
                )
            }
            kbank_matches_by_row_order = {
                item["rv_bank_row"].get("row_order"): item
                for item in find_rv_kbank_candidates(
                    conn,
                    voucher_number=row["voucher_number"],
                    kbank_deposit_rows_by_check=kbank_deposit_rows_by_check,
                    kbank_deposit_rows_by_amount=kbank_deposit_rows_by_amount,
                )
            }
            for bank_row in bank_rows:
                row_order = bank_row.get("row_order")
                if row_order in matches_by_row_order:
                    bank_row["uob_match"] = matches_by_row_order[row_order]
                if row_order in kbank_matches_by_row_order:
                    bank_row["kbank_match"] = kbank_matches_by_row_order[row_order]
            row["bank_rows"] = bank_rows

    return rows


@router.get("/{voucher_number}/pdf")
def get_receive_voucher_pdf(voucher_number: str):
    with db_session() as conn:
        row = fetch_document_pdf(conn, "receive-vouchers", normalize_ref(voucher_number))

    if not row:
        raise HTTPException(status_code=404, detail="Voucher Number not found")

    pdf_bytes = read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"])
    headers = {"Content-Disposition": f'inline; filename="{normalize_ref(voucher_number)}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/export/by-filter/pdf")
def export_receive_vouchers_pdf(
    voucher_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    vouchers = search_receive_vouchers(
        voucher_number=voucher_number,
        date_from=date_from,
        date_to=date_to,
        ref_type=ref_type,
        ref_value=ref_value,
    )

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers match the filter")

    pdf_parts: list[bytes] = []
    with db_session() as conn:
        for voucher in vouchers:
            row = fetch_document_pdf(conn, "receive-vouchers", voucher["voucher_number"])
            pdf_parts.append(read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"]))

    output_pdf = assemble_pdf(pdf_parts)
    headers = {"Content-Disposition": 'inline; filename="receive_vouchers_export.pdf"'}
    return Response(content=output_pdf, media_type="application/pdf", headers=headers)
