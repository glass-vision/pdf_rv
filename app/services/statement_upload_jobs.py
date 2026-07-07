from __future__ import annotations

import json
from sqlite3 import Connection
from typing import Any

from app.services.statement_importer import StatementImportResult
from app.services.upload_job_common import (
    get_upload_job as common_get_upload_job,
    list_active_upload_jobs as common_list_active_upload_jobs,
    list_upload_job_history as common_list_upload_job_history,
    request_upload_job_cancel as common_request_upload_job_cancel,
    utc_now,
)


CANCELLED_MESSAGE = "Cancelled by user"


class UploadCancelled(Exception):
    pass


def create_upload_job(
    conn: Connection,
    table: str,
    job_id: str,
    filename: str,
    client_id: str = "",
    doc_type: str = "",
) -> None:
    now = utc_now()
    conn.execute(
        f"INSERT INTO {table} (id, doc_type, filename, client_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (job_id, doc_type, filename, client_id, now, now),
    )


def mark_upload_job_processing(
    conn: Connection, table: str, job_id: str, total_pages: int
) -> None:
    conn.execute(
        f"UPDATE {table} SET status = 'processing', total_pages = ?, "
        "processed_pages = 0, updated_at = ? WHERE id = ?",
        (total_pages, utc_now(), job_id),
    )


def update_upload_job_progress(
    conn: Connection, table: str, job_id: str, processed_pages: int
) -> None:
    conn.execute(
        f"UPDATE {table} SET processed_pages = ?, updated_at = ? WHERE id = ?",
        (processed_pages, utc_now(), job_id),
    )


def mark_upload_job_done(
    conn: Connection,
    table: str,
    keys_column: str,
    job_id: str,
    result: StatementImportResult,
) -> None:
    conn.execute(
        f"""
        UPDATE {table}
        SET status = 'done', total_pages = ?, processed_pages = ?,
            statement_count = ?, {keys_column} = ?, warnings = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            result.total_pages,
            result.total_pages,
            result.statement_count,
            json.dumps(result.statement_keys),
            json.dumps(result.warnings),
            utc_now(),
            job_id,
        ),
    )


def mark_upload_job_failed(
    conn: Connection, table: str, job_id: str, error_message: str
) -> None:
    conn.execute(
        f"UPDATE {table} SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
        (error_message, utc_now(), job_id),
    )


def request_upload_job_cancel(conn: Connection, table: str, job_id: str, client_id: str = "") -> bool:
    return common_request_upload_job_cancel(conn, table, job_id, client_id)


def is_upload_job_cancel_requested(conn: Connection, table: str, job_id: str) -> bool:
    row = conn.execute(
        f"SELECT cancel_requested FROM {table} WHERE id = ?", (job_id,)
    ).fetchone()
    return bool(row and row["cancel_requested"])


def mark_upload_job_cancelled(conn: Connection, table: str, job_id: str) -> None:
    mark_upload_job_failed(conn, table, job_id, CANCELLED_MESSAGE)


def fail_stale_upload_jobs(conn: Connection, table: str) -> None:
    conn.execute(
        f"UPDATE {table} SET status = 'failed', "
        "error_message = 'Upload interrupted', updated_at = ? "
        "WHERE status IN ('pending', 'processing')",
        (utc_now(),),
    )


def get_upload_job(
    conn: Connection, table: str, keys_column: str, job_id: str, doc_type: str
) -> dict[str, Any] | None:
    return common_get_upload_job(conn, table, job_id, (keys_column, "warnings"), doc_type)


def list_active_upload_jobs(
    conn: Connection, table: str, keys_column: str, doc_type: str
) -> list[dict[str, Any]]:
    return common_list_active_upload_jobs(conn, table, (keys_column, "warnings"), doc_type)


def list_upload_job_history(
    conn: Connection, table: str, keys_column: str, doc_type: str, limit: int = 20
) -> list[dict[str, Any]]:
    return common_list_upload_job_history(conn, table, (keys_column, "warnings"), limit, doc_type)
