from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_job(row: Any, json_fields: tuple[str, ...]) -> dict[str, Any]:
    job = dict(row)
    for field in json_fields:
        job[field] = json.loads(job[field]) if job[field] else None
    return job


def get_upload_job(
    conn: Connection,
    table: str,
    job_id: str,
    json_fields: tuple[str, ...],
    doc_type: str | None = None,
) -> dict[str, Any] | None:
    if doc_type is None:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?",
            (job_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND doc_type = ?",
            (job_id, doc_type),
        ).fetchone()
    if not row:
        return None
    return row_to_job(row, json_fields)


def list_active_upload_jobs(
    conn: Connection,
    table: str,
    json_fields: tuple[str, ...],
    doc_type: str | None = None,
) -> list[dict[str, Any]]:
    if doc_type is None:
        rows = conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE status IN ('pending', 'processing')
            ORDER BY created_at ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE doc_type = ? AND status IN ('pending', 'processing')
            ORDER BY created_at ASC
            """,
            (doc_type,),
        ).fetchall()
    return [row_to_job(row, json_fields) for row in rows]


def list_upload_job_history(
    conn: Connection,
    table: str,
    json_fields: tuple[str, ...],
    limit: int = 20,
    doc_type: str | None = None,
) -> list[dict[str, Any]]:
    if doc_type is None:
        rows = conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE status IN ('done', 'failed')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE doc_type = ? AND status IN ('done', 'failed')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (doc_type, limit),
        ).fetchall()
    return [row_to_job(row, json_fields) for row in rows]


def request_upload_job_cancel(
    conn: Connection,
    table: str,
    job_id: str,
    client_id: str = "",
) -> bool:
    cursor = conn.execute(
        f"""
        UPDATE {table}
        SET cancel_requested = 1, updated_at = ?
        WHERE id = ? AND status IN ('pending', 'processing') AND (client_id = ? OR client_id = '')
        """,
        (utc_now(), job_id, client_id),
    )
    return cursor.rowcount > 0
