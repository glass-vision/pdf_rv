from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Annotated, Callable
from uuid import uuid4

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader

from app.config import get_settings
from app.database import db_session
from app.services.document_core import (
    fetch_document_page_by_order,
    fetch_document_pdf,
    fetch_document_pages_with_refs,
    fetch_document_refs,
    fetch_document_root,
    search_document_roots,
)
from app.services.browser_identity import normalize_client_id
from app.services.pdf_assembler import assemble_pdf
from app.services.statement_importer import StatementImportResult
from app.services.statement_upload_jobs import (
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
from app.services.storage import read_pdf_from_storage


def normalize_date_filter(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise HTTPException(status_code=422, detail="Date filters must use YYYY-MM-DD or DD/MM/YYYY")


def create_statement_router(
    *,
    prefix: str,
    tag: str,
    key_column: str,
    jobs_table: str,
    job_keys_column: str,
    doc_type: str,
    allowed_ref_types: set[str],
    importer: Callable[..., StatementImportResult],
    normalizer: Callable[[str], str],
    export_filename: str,
    page_extractor: Callable[[str], Any | None] | None = None,
    # legacy params kept for call-site compat — ignored
    table: str = "",
    refs_table: str = "",
    foreign_key: str = "",
    select_columns: list[str] | None = None,
    search_columns: set[str] | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])

    def validate_refs(ref_type: str | None, ref_value: str | None) -> None:
        if bool(ref_type) != bool(ref_value):
            raise HTTPException(status_code=422, detail="ref_type and ref_value must be provided together")
        if ref_type and ref_type not in allowed_ref_types:
            raise HTTPException(status_code=422, detail=f"Unsupported ref_type: {ref_type}")

    def process_upload(job_id: str, temp_path: Path) -> None:
        try:
            pdf_bytes = temp_path.read_bytes()
            total_pages = len(PdfReader(BytesIO(pdf_bytes)).pages)
            with db_session() as conn:
                mark_upload_job_processing(conn, jobs_table, job_id, total_pages)

            last_progress_page = 0

            def on_page(page_number: int) -> None:
                with db_session() as conn:
                    if is_upload_job_cancel_requested(conn, jobs_table, job_id):
                        raise UploadCancelled()
                    nonlocal last_progress_page
                    should_flush = (
                        page_number == total_pages
                        or page_number - last_progress_page >= 8
                        or last_progress_page == 0
                    )
                    if should_flush:
                        update_upload_job_progress(conn, jobs_table, job_id, page_number)
                        last_progress_page = page_number

            with db_session() as conn:
                result = importer(conn, pdf_bytes, on_page=on_page)
                mark_upload_job_done(conn, jobs_table, job_keys_column, job_id, result)
        except UploadCancelled:
            with db_session() as conn:
                mark_upload_job_cancelled(conn, jobs_table, job_id)
        except Exception as exc:
            with db_session() as conn:
                mark_upload_job_failed(conn, jobs_table, job_id, str(exc))
        finally:
            temp_path.unlink(missing_ok=True)

    @router.post("/upload", status_code=202)
    async def upload(
        file: UploadFile = File(...),
        x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ):
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are supported")
        job_id = str(uuid4())
        temp_dir = get_settings().resolved_app_data_dir / "uploads_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = (temp_dir / f"{job_id}.pdf").resolve()
        temp_path.write_bytes(await file.read())
        client_id = normalize_client_id(x_client_id)
        with db_session() as conn:
            create_upload_job(conn, jobs_table, job_id, file.filename, client_id, tag)
        enqueue_upload_task(
            tag,
            job_id,
            temp_path,
            process_upload,
            provenance={
                "client_id": client_id,
                "source_page": prefix.replace("/api", ""),
                "source_action": "upload",
                "source_file_name": file.filename,
            },
        )
        return {"status": "pending", "job_id": job_id, "filename": file.filename}

    @router.get("/upload/active")
    def active_jobs(x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None):
        with db_session() as conn:
            return list_active_upload_jobs(conn, jobs_table, job_keys_column, tag)

    @router.get("/upload/history")
    def upload_history(
        limit: int = 20,
        x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ):
        with db_session() as conn:
            return list_upload_job_history(conn, jobs_table, job_keys_column, tag, limit)

    @router.post("/upload/{job_id}/cancel")
    def cancel_upload(
        job_id: str,
        x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ):
        client_id = normalize_client_id(x_client_id)
        with db_session() as conn:
            job = get_upload_job(conn, jobs_table, job_keys_column, job_id, tag)
            if not job:
                raise HTTPException(status_code=404, detail="Upload job not found")
            if job.get("client_id") not in ("", client_id):
                raise HTTPException(status_code=403, detail="Upload job belongs to another browser")
            if job["status"] not in ("pending", "processing"):
                raise HTTPException(status_code=409, detail="Upload job already finished")
            request_upload_job_cancel(conn, jobs_table, job_id, client_id)
        request_upload_task_cancel(tag, job_id)
        return {"status": "cancel_requested"}

    @router.get("/upload/{job_id}")
    def upload_status(
        job_id: str,
        x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ):
        with db_session() as conn:
            job = get_upload_job(conn, jobs_table, job_keys_column, job_id, tag)
        if not job:
            raise HTTPException(status_code=404, detail="Upload job not found")
        return job

    def search_impl(
        filters: dict[str, str | None],
        period_from: str | None,
        period_to: str | None,
        ref_type: str | None,
        ref_value: str | None,
    ) -> list[dict[str, Any]]:
        validate_refs(ref_type, ref_value)
        root_key_filter = filters.get(key_column)
        account_filter = filters.get("account_number") or filters.get("company_id")
        with db_session() as conn:
            rows = [dict(row) for row in search_document_roots(
                conn,
                doc_type=doc_type,
                root_key=normalizer(root_key_filter) if root_key_filter else None,
                root_date_from=normalize_date_filter(period_from) if period_from else None,
                period_from_max=normalize_date_filter(period_to) if period_to else None,
                customer_code=normalizer(account_filter) if account_filter else None,
                ref_type=ref_type,
                ref_value=normalizer(ref_value) if ref_value else None,
            )]
            for row in rows:
                row[key_column] = row.pop("root_key")
                row["account_number"] = row.get("customer_code")
                refs = fetch_document_refs(conn, row["id"])
                # root_date holds period_to when present, which is wrong
                # whenever a page has no period_to but does have a
                # statement_date/period_from (root_date then falls through
                # to those instead). Existing imports predate this ref, so
                # fall back to the old (occasionally wrong) alias for them.
                row["period_to"] = next(iter(refs.get("period_to", [])), row.get("root_date"))
                # root_date holds period_to when present, which is wrong for
                # UOB's statement_date (the print date, not the period end)
                # whenever they differ. Existing imports predate this ref, so
                # fall back to the old (occasionally wrong) alias for them.
                row["statement_date"] = next(iter(refs.get("statement_date", [])), row.get("root_date"))
                # Existing imports predate the account_name ref. Keep their
                # root-level name visible until those statements are re-uploaded.
                row["account_name"] = next(iter(refs.get("account_name", [])), row.get("name"))
                row["branch_name"] = next(iter(refs.get("branch_name", [])), None)
                row["refs"] = {rt: ", ".join(vs) for rt, vs in refs.items()}
        return rows

    router.state = type("RouterState", (), {})()
    router.state.search_impl = search_impl

    @router.get("/{statement_key}.pdf")
    @router.get("/{statement_key}/pdf")
    def get_pdf(statement_key: str):
        with db_session() as conn:
            row = fetch_document_pdf(conn, doc_type, normalizer(statement_key))
        if not row:
            raise HTTPException(status_code=404, detail="Statement not found")
        pdf_bytes = read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"])
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{statement_key}.pdf"'},
        )

    @router.get("/{statement_key}/pages")
    def get_pages(statement_key: str):
        normalized_key = normalizer(statement_key)
        with db_session() as conn:
            root = fetch_document_root(conn, doc_type, normalized_key)
            if not root:
                raise HTTPException(status_code=404, detail="Statement not found")
            pages = fetch_document_pages_with_refs(conn, root["id"])
            kbank_check_rows_by_page: dict[int, list[dict[str, Any]]] = {}
            if doc_type == "kbank-statements":
                check_rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT page_order, event_type, check_no, txn_date, amount_raw, amount,
                               balance_raw, balance, bank_hint, source_line
                        FROM kbank_check_rows
                        WHERE document_root_id = ?
                        ORDER BY page_order, row_order
                        """,
                        (root["id"],),
                    ).fetchall()
                ]
                for row in check_rows:
                    kbank_check_rows_by_page.setdefault(int(row.pop("page_order")), []).append(row)

        key_name = "statement_reference" if doc_type == "kbank-statements" else "statement_key"
        payload = []
        for page in pages:
            refs = page.get("refs", {}) or {}
            if not refs and page_extractor and page.get("raw_text"):
                extracted = page_extractor(page["raw_text"])
                if extracted and getattr(extracted, "refs", None):
                    refs = {
                        ref_type: sorted(values)
                        for ref_type, values in extracted.refs.items()
                        if values
                    }
            payload.append(
                {
                    "page_key": f"{normalized_key}:{page['page_order']}",
                    key_name: normalized_key,
                    "page_order": page["page_order"],
                    "page_count": 1,
                    "refs": {rt: ", ".join(vs) for rt, vs in refs.items()},
                    "raw_text_preview": (page.get("raw_text") or "")[:800],
                }
            )
            if doc_type == "kbank-statements":
                payload[-1]["check_rows"] = kbank_check_rows_by_page.get(page["page_order"], [])
        return payload

    def _parse_page_key(raw_key: str) -> tuple[str, int]:
        root_part, sep, page_part = raw_key.rpartition(":")
        if not sep or not root_part or not page_part.isdigit():
            raise HTTPException(status_code=422, detail=f"Invalid page_key: {raw_key}")
        return normalizer(root_part), int(page_part)

    def _fetch_page_pdf_bytes(conn, raw_key: str) -> tuple[str, int, bytes]:
        normalized_key, page_order = _parse_page_key(raw_key)
        root = fetch_document_root(conn, doc_type, normalized_key)
        if not root:
            raise HTTPException(status_code=404, detail=f"Statement not found for page_key: {raw_key}")
        page = fetch_document_page_by_order(conn, root["id"], page_order)
        if not page:
            raise HTTPException(status_code=404, detail=f"Page not found for page_key: {raw_key}")
        pdf_bytes = read_pdf_from_storage(page["page_pdf"], page["page_pdf_path"])
        return normalized_key, page_order, pdf_bytes

    @router.get("/pages/{page_key}.pdf")
    @router.get("/pages/{page_key}/pdf")
    def get_page_pdf(page_key: str):
        with db_session() as conn:
            normalized_key, page_order, pdf_bytes = _fetch_page_pdf_bytes(conn, page_key)
        filename = f"{normalized_key}:{page_order}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    @router.get("/page-selection/export/pdf")
    def export_pages_pdf(keys: str):
        requested_keys = [item.strip() for item in keys.split(",") if item.strip()]
        if not requested_keys:
            raise HTTPException(status_code=422, detail="At least one page_key is required")
        with db_session() as conn:
            parts = []
            normalized_keys = []
            for raw_key in requested_keys:
                normalized_key, page_order, pdf_bytes = _fetch_page_pdf_bytes(conn, raw_key)
                parts.append(pdf_bytes)
                normalized_keys.append(f"{normalized_key}:{page_order}")
        filename = f"{tag.replace('-', '_')}_pages.pdf"
        return Response(
            content=assemble_pdf(parts),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    def export_impl(rows: list[dict[str, Any]]) -> Response:
        if not rows:
            raise HTTPException(status_code=404, detail="No statements match the filter")
        with db_session() as conn:
            parts = []
            for row in rows:
                stored = fetch_document_pdf(conn, doc_type, row.get(key_column))
                if not stored:
                    raise HTTPException(status_code=404, detail="Statement not found")
                parts.append(read_pdf_from_storage(stored["assembled_pdf"], stored["assembled_pdf_path"]))
        return Response(
            content=assemble_pdf(parts),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{export_filename}"'},
        )

    router.state.export_impl = export_impl
    return router
