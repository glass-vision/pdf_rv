from __future__ import annotations

import json
from sqlite3 import Connection
from typing import Any

from app.services.receive_voucher_importer import ImportResult
from app.services.upload_job_common import (
    get_upload_job as common_get_upload_job,
    list_active_upload_jobs as common_list_active_upload_jobs,
    list_upload_job_history as common_list_upload_job_history,
    request_upload_job_cancel as common_request_upload_job_cancel,
    utc_now,
)


def create_upload_job(
    conn: Connection,
    job_id: str,
    filename: str,
    client_id: str = "",
    doc_type: str = "receive-vouchers",
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO upload_jobs (id, doc_type, filename, client_id, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (job_id, doc_type, filename, client_id, now, now),
    )


def mark_upload_job_processing(conn: Connection, job_id: str, total_pages: int) -> None:
    conn.execute(
        """
        UPDATE upload_jobs
        SET status = 'processing', total_pages = ?, processed_pages = 0, updated_at = ?
        WHERE id = ?
        """,
        (total_pages, utc_now(), job_id),
    )


def update_upload_job_progress(conn: Connection, job_id: str, processed_pages: int) -> None:
    conn.execute(
        "UPDATE upload_jobs SET processed_pages = ?, updated_at = ? WHERE id = ?",
        (processed_pages, utc_now(), job_id),
    )


def mark_upload_job_done(conn: Connection, job_id: str, result: ImportResult) -> None:
    conn.execute(
        """
        UPDATE upload_jobs
        SET status = 'done',
            total_pages = ?,
            processed_pages = ?,
            voucher_count = ?,
            voucher_numbers = ?,
            warnings = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            result.total_pages,
            result.total_pages,
            result.voucher_count,
            json.dumps(result.voucher_numbers),
            json.dumps(result.warnings),
            utc_now(),
            job_id,
        ),
    )


def mark_upload_job_failed(conn: Connection, job_id: str, error_message: str) -> None:
    conn.execute(
        """
        UPDATE upload_jobs
        SET status = 'failed', error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (error_message, utc_now(), job_id),
    )


CANCELLED_MESSAGE = "Cancelled by user"


class UploadCancelled(Exception):
    """Raised from the on_page callback to abort a cancelled upload job."""


def request_upload_job_cancel(conn: Connection, job_id: str, client_id: str = "") -> bool:
    """Flag a pending/processing job for cancellation.

    Returns True if a job was flagged, False if it doesn't exist or has
    already finished.
    """
    return common_request_upload_job_cancel(conn, "upload_jobs", job_id, client_id)


def is_upload_job_cancel_requested(conn: Connection, job_id: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM upload_jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def mark_upload_job_cancelled(conn: Connection, job_id: str) -> None:
    conn.execute(
        """
        UPDATE upload_jobs
        SET status = 'failed', error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (CANCELLED_MESSAGE, utc_now(), job_id),
    )


def fail_stale_upload_jobs(conn: Connection) -> None:
    """Fail any jobs left 'pending'/'processing' from a previous run.

    If the app process was restarted or a background worker was lost, these
    jobs are no longer reliable. Mark them interrupted so the UI stops
    treating them as active.
    """
    conn.execute(
        """
        UPDATE upload_jobs
        SET status = 'failed', error_message = 'Upload interrupted', updated_at = ?
        WHERE status IN ('pending', 'processing')
        """,
        (utc_now(),),
    )


def get_upload_job(conn: Connection, job_id: str) -> dict[str, Any] | None:
    return common_get_upload_job(
        conn,
        "upload_jobs",
        job_id,
        ("voucher_numbers", "warnings"),
        "receive-vouchers",
    )


def list_active_upload_jobs(conn: Connection) -> list[dict[str, Any]]:
    """All jobs that are still pending or processing, oldest first."""
    return common_list_active_upload_jobs(
        conn,
        "upload_jobs",
        ("voucher_numbers", "warnings"),
        "receive-vouchers",
    )


def list_upload_job_history(conn: Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent finished jobs (done/failed), newest first."""
    return common_list_upload_job_history(
        conn,
        "upload_jobs",
        ("voucher_numbers", "warnings"),
        limit,
        "receive-vouchers",
    )
