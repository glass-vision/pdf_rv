from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

from app.config import get_settings
from app.database import db_session
from app.services.document_core import fetch_document_pdf, fetch_document_refs, search_document_roots
from app.services.browser_identity import normalize_client_id
from app.services.credit_memo_extractor import normalize_ref
from app.services.credit_memo_importer import import_credit_memo_pdf
from app.services.credit_memo_upload_jobs import (
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
from app.services.pdf_assembler import assemble_pdf
from app.services.storage import read_pdf_from_storage
from app.services.upload_worker import enqueue_upload_task, request_upload_task_cancel


router = APIRouter(prefix="/api/credit-memos", tags=["credit-memos"])

ALLOWED_REF_TYPES = {
    "invoice_no",
    "customer_code",
}

CREDIT_MEMO_DATE_DISPLAY_SQL = (
    "CASE "
    "WHEN cm.credit_memo_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
    "THEN substr(cm.credit_memo_date, 9, 2) || '/' || substr(cm.credit_memo_date, 6, 2) || '/' || substr(cm.credit_memo_date, 1, 4) "
    "ELSE cm.credit_memo_date "
    "END"
)

REF_TYPE_ORDER_SQL = (
    "CASE ref_type "
    "WHEN 'customer_code' THEN 10 "
    "WHEN 'invoice_no' THEN 20 "
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
            result = import_credit_memo_pdf(conn, pdf_bytes, on_page=on_page)
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
async def upload_credit_memos(
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
        create_upload_job(conn, job_id, file.filename, client_id, "credit-memos")

    enqueue_upload_task(
        "credit-memos",
        job_id,
        temp_path,
        _process_upload_job,
        provenance={
            "client_id": client_id,
            "source_page": "/credit-memos",
            "source_action": "upload",
            "source_file_name": file.filename,
        },
    )

    return {"status": "pending", "job_id": job_id, "filename": file.filename}


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
    client_id = normalize_client_id(x_client_id)
    with db_session() as conn:
        job = get_upload_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Upload job not found")
        if job.get("client_id") not in ("", client_id):
            raise HTTPException(status_code=403, detail="Upload job belongs to another browser")
        if job["status"] not in ("pending", "processing"):
            raise HTTPException(status_code=409, detail="Upload job already finished")
        request_upload_job_cancel(conn, job_id, client_id)
    request_upload_task_cancel("credit-memos", job_id)

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
def search_credit_memos(
    credit_memo_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    validate_ref_filter(ref_type, ref_value)
    with db_session() as conn:
        rows = [dict(row) for row in search_document_roots(
            conn,
            doc_type="credit-memos",
            root_key=normalize_ref(credit_memo_number) if credit_memo_number else None,
            root_date_from=normalize_date_filter(date_from) if date_from else None,
            root_date_to=normalize_date_filter(date_to) if date_to else None,
            ref_type=ref_type,
            ref_value=normalize_ref(ref_value) if ref_value else None,
        )]
        for row in rows:
            row["credit_memo_number"] = row.pop("root_key")
            iso = row.get("root_date")
            row["credit_memo_date_iso"] = iso
            row["credit_memo_date"] = (
                f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) == 10 else iso
            )
            refs = fetch_document_refs(conn, row["id"])
            row["refs"] = {rt: ", ".join(vs) for rt, vs in refs.items()}

    return rows


@router.get("/{credit_memo_number}.pdf")
@router.get("/{credit_memo_number}/pdf")
def get_credit_memo_pdf(credit_memo_number: str):
    with db_session() as conn:
        row = fetch_document_pdf(conn, "credit-memos", normalize_ref(credit_memo_number))

    if not row:
        raise HTTPException(status_code=404, detail="Credit Memo No. not found")

    pdf_bytes = read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"])
    headers = {"Content-Disposition": f'inline; filename="{normalize_ref(credit_memo_number)}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/export/by-filter/pdf")
def export_credit_memos_pdf(
    credit_memo_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    credit_memos = search_credit_memos(
        credit_memo_number=credit_memo_number,
        date_from=date_from,
        date_to=date_to,
        ref_type=ref_type,
        ref_value=ref_value,
    )

    if not credit_memos:
        raise HTTPException(status_code=404, detail="No credit memos match the filter")

    pdf_parts: list[bytes] = []
    with db_session() as conn:
        for credit_memo in credit_memos:
            row = fetch_document_pdf(conn, "credit-memos", credit_memo["credit_memo_number"])
            if not row:
                raise HTTPException(status_code=404, detail="Credit Memo No. not found")
            pdf_parts.append(read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"]))

    output_pdf = assemble_pdf(pdf_parts)
    headers = {"Content-Disposition": 'inline; filename="credit_memos_export.pdf"'}
    return Response(content=output_pdf, media_type="application/pdf", headers=headers)
