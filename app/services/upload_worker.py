from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection
from threading import Event, Lock, Thread
from time import sleep
from typing import Any, Callable

from app.config import get_settings
from app.database import db_session


LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 10
RECOVERY_SECONDS = 5
DEFAULT_WORKER_COUNT = 2
MAX_WORKER_COUNT = 4
CANCELLED_MESSAGE = "Cancelled by user"
INTERRUPTED_MESSAGE = "Upload interrupted"
RETRYABLE_ERROR_HINTS = (
    "database is locked",
    "connection failed",
    "upload interrupted",
    "interrupted by server restart",
)

JOB_TABLE = "upload_jobs"

UploadHandler = Callable[[str, Path], None]


@dataclass(slots=True)
class QueuedUploadJob:
    id: str
    job_type: str
    payload_json: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    available_at: str
    claimed_by: str | None
    claimed_at: str | None
    heartbeat_at: str | None
    lease_expires_at: str | None
    last_error: str | None
    cancel_requested: int
    created_at: str
    updated_at: str

    @property
    def payload(self) -> dict[str, Any]:
        try:
            return json.loads(self.payload_json)
        except Exception:
            return {}

    @property
    def temp_path(self) -> Path | None:
        temp_path = self.payload.get("temp_path")
        if not temp_path:
            return None
        return Path(str(temp_path)).expanduser().resolve()

    @property
    def provenance(self) -> dict[str, Any]:
        value = self.payload.get("provenance")
        return value if isinstance(value, dict) else {}


_HANDLERS: dict[str, UploadHandler] = {}
_HANDLER_LOCK = Lock()
_STOP_EVENT = Event()
_WORKER_THREADS: list[Thread] = []
_RECOVERY_THREAD: Thread | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _job_table_for(job_type: str) -> str:
    if not job_type:
        raise ValueError("Unsupported job type")
    return JOB_TABLE


def _get_handler(job_type: str) -> UploadHandler:
    with _HANDLER_LOCK:
        try:
            return _HANDLERS[job_type]
        except KeyError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(f"No upload handler registered for {job_type}") from exc


def register_upload_handler(job_type: str, handler: UploadHandler) -> None:
    with _HANDLER_LOCK:
        _HANDLERS[job_type] = handler


def _row_to_job(row: Any) -> QueuedUploadJob:
    return QueuedUploadJob(
        id=row["id"],
        job_type=row["job_type"],
        payload_json=row["payload_json"],
        status=row["status"],
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        heartbeat_at=row["heartbeat_at"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
        cancel_requested=row["cancel_requested"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _retryable_error(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(hint in lowered for hint in RETRYABLE_ERROR_HINTS)


def _get_document_job(conn, job_type: str, job_id: str) -> Any | None:
    table = _job_table_for(job_type)
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (job_id,)).fetchone()


def _update_document_job_state(
    conn,
    job_type: str,
    job_id: str,
    *,
    status: str | None = None,
    processed_pages: int | None = None,
    error_message: str | None = None,
    cancel_requested: int | None = None,
) -> None:
    table = _job_table_for(job_type)
    assignments: list[str] = []
    values: list[Any] = []

    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    if processed_pages is not None:
        assignments.append("processed_pages = ?")
        values.append(processed_pages)
    if error_message is not None:
        assignments.append("error_message = ?")
        values.append(error_message)
    if cancel_requested is not None:
        assignments.append("cancel_requested = ?")
        values.append(cancel_requested)

    if not assignments:
        return

    assignments.append("updated_at = ?")
    values.append(utc_now())
    values.append(job_id)

    conn.execute(
        f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?",
        values,
    )


def _reset_document_job_for_retry(conn, job_type: str, job_id: str) -> None:
    _update_document_job_state(
        conn,
        job_type,
        job_id,
        status="pending",
        processed_pages=0,
        error_message=None,
        cancel_requested=0,
    )


def _mark_document_job_cancelled(conn, job_type: str, job_id: str) -> None:
    _update_document_job_state(
        conn,
        job_type,
        job_id,
        status="failed",
        error_message=CANCELLED_MESSAGE,
        cancel_requested=1,
    )


def _mark_document_job_failed(conn, job_type: str, job_id: str, error_message: str) -> None:
    _update_document_job_state(
        conn,
        job_type,
        job_id,
        status="failed",
        error_message=error_message,
    )


def _run_queue_update(conn: Connection | None, sql: str, params: tuple[Any, ...]) -> None:
    if conn is not None:
        conn.execute(sql, params)
        return

    with db_session() as temp_conn:
        temp_conn.execute(sql, params)


def _execute_claimed_upload_job(job: QueuedUploadJob, worker_id: str) -> None:
    handler = _get_handler(job.job_type)
    heartbeat_stop = Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            if _STOP_EVENT.is_set():
                return
            heartbeat_upload_task(job.id, worker_id)

    heartbeat_thread = Thread(
        target=heartbeat_loop,
        name=f"upload-heartbeat-{worker_id}",
        daemon=True,
    )
    heartbeat_thread.start()

    temp_path = job.temp_path
    try:
        if temp_path is None:
            raise FileNotFoundError("Queue payload missing temp_path")

        with db_session() as conn:
            queued = conn.execute(
                "SELECT cancel_requested FROM upload_job_queue WHERE id = ?",
                (job.id,),
            ).fetchone()

        if queued and queued["cancel_requested"]:
            with db_session() as conn:
                _mark_document_job_cancelled(conn, job.job_type, job.id)
            _mark_queue_cancelled(job.id, worker_id)
            return

        handler(job.id, temp_path)
        _finalize_queue_from_document_state(job, worker_id)
    except Exception as exc:  # pragma: no cover - defensive logging only
        traceback.print_exc()
        with db_session() as conn:
            doc = _get_document_job(conn, job.job_type, job.id)
            message = str(exc)
            if doc and doc["status"] == "failed" and _retryable_error(doc["error_message"]) and job.attempt_count < job.max_attempts:
                _reset_document_job_for_retry(conn, job.job_type, job.id)
                _mark_queue_retry_wait(job.id, worker_id, doc["error_message"] or message, job.attempt_count, conn=conn)
            elif doc and doc["status"] == "failed" and doc["error_message"] == CANCELLED_MESSAGE:
                _mark_queue_cancelled(job.id, worker_id, conn=conn)
            elif doc and doc["status"] == "done":
                _mark_queue_done(job.id, worker_id, conn=conn)
            elif job.attempt_count < job.max_attempts and _retryable_error(message):
                _reset_document_job_for_retry(conn, job.job_type, job.id)
                _mark_queue_retry_wait(job.id, worker_id, message, job.attempt_count, conn=conn)
            else:
                _mark_document_job_failed(conn, job.job_type, job.id, message)
                _mark_queue_failed(job.id, worker_id, message, conn=conn)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        try:
            with db_session() as conn:
                queue_row = conn.execute(
                    "SELECT status FROM upload_job_queue WHERE id = ?",
                    (job.id,),
                ).fetchone()

            if queue_row and queue_row["status"] in {"done", "failed", "cancelled"}:
                temp_path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - best-effort cleanup
            pass


def enqueue_upload_task(
    job_type: str,
    job_id: str,
    temp_path: Path,
    handler: UploadHandler,
    *,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Persist a long-running upload import so workers can claim it later."""
    register_upload_handler(job_type, handler)
    payload_data: dict[str, Any] = {"temp_path": str(temp_path)}
    if provenance:
        payload_data["provenance"] = provenance
    payload = json.dumps(payload_data)
    now = utc_now()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        stop_upload_worker()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO upload_job_queue (
                id, job_type, payload_json, status, priority, attempt_count,
                max_attempts, available_at, claimed_by, claimed_at, heartbeat_at,
                lease_expires_at, last_error, cancel_requested, created_at, updated_at
            )
            VALUES (?, ?, ?, 'pending', 100, 0, 3, ?, NULL, NULL, NULL, NULL, NULL, 0, ?, ?)
            """,
            (job_id, job_type, payload, now, now, now),
        )
    if os.environ.get("PYTEST_CURRENT_TEST"):
        stop_upload_worker()
        job = claim_next_upload_task("inline")
        if job is not None:
            _execute_claimed_upload_job(job, "inline")
        return
    start_upload_worker(get_settings().upload_worker_count)


def request_upload_task_cancel(job_type: str, job_id: str) -> bool:
    with db_session() as conn:
        cursor = conn.execute(
            """
            UPDATE upload_job_queue
            SET cancel_requested = 1, updated_at = ?
            WHERE id = ? AND job_type = ? AND status IN ('pending', 'running', 'retry_wait')
            """,
            (utc_now(), job_id, job_type),
        )
        return cursor.rowcount > 0


def claim_next_upload_task(worker_id: str) -> QueuedUploadJob | None:
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = utc_now()
        row = conn.execute(
            """
            SELECT *
            FROM upload_job_queue
            WHERE status IN ('pending', 'retry_wait')
              AND available_at <= ?
              AND cancel_requested = 0
            ORDER BY priority ASC, created_at ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None

        lease_expires_at = _iso_after(LEASE_SECONDS)
        cursor = conn.execute(
            """
            UPDATE upload_job_queue
            SET status = 'running',
                attempt_count = attempt_count + 1,
                claimed_by = ?,
                claimed_at = ?,
                heartbeat_at = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'retry_wait')
              AND available_at <= ?
              AND cancel_requested = 0
            """,
            (worker_id, now, now, lease_expires_at, now, row["id"], now),
        )
        if cursor.rowcount == 0:
            return None

        job = _row_to_job(row)
        job.attempt_count += 1
        job.status = "running"
        job.claimed_by = worker_id
        job.claimed_at = now
        job.heartbeat_at = now
        job.lease_expires_at = lease_expires_at
        return job


def heartbeat_upload_task(job_id: str, worker_id: str) -> None:
    now = utc_now()
    with db_session() as conn:
        conn.execute(
            """
            UPDATE upload_job_queue
            SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND claimed_by = ? AND status = 'running'
            """,
            (now, _iso_after(LEASE_SECONDS), now, job_id, worker_id),
        )


def _mark_queue_done(job_id: str, worker_id: str, conn: Connection | None = None) -> None:
    _run_queue_update(
        conn,
        """
        UPDATE upload_job_queue
        SET status = 'done',
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            last_error = NULL,
            updated_at = ?
        WHERE id = ? AND claimed_by = ? AND status = 'running'
        """,
        (utc_now(), job_id, worker_id),
    )


def _mark_queue_cancelled(job_id: str, worker_id: str | None = None, conn: Connection | None = None) -> None:
    if worker_id is None:
        _run_queue_update(
            conn,
            """
            UPDATE upload_job_queue
            SET status = 'cancelled',
                claimed_by = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL,
                lease_expires_at = NULL,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (CANCELLED_MESSAGE, utc_now(), job_id),
        )
        return

    _run_queue_update(
        conn,
        """
        UPDATE upload_job_queue
        SET status = 'cancelled',
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            last_error = ?,
            updated_at = ?
        WHERE id = ? AND claimed_by = ? AND status = 'running'
        """,
        (CANCELLED_MESSAGE, utc_now(), job_id, worker_id),
    )


def _mark_queue_failed(job_id: str, worker_id: str, error_message: str, conn: Connection | None = None) -> None:
    _run_queue_update(
        conn,
        """
        UPDATE upload_job_queue
        SET status = 'failed',
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            last_error = ?,
            updated_at = ?
        WHERE id = ? AND claimed_by = ? AND status = 'running'
        """,
        (error_message, utc_now(), job_id, worker_id),
    )


def _mark_queue_retry_wait(
    job_id: str,
    worker_id: str,
    error_message: str,
    attempt_count: int,
    conn: Connection | None = None,
) -> None:
    delay_seconds = min(60, 5 * (2**max(0, attempt_count - 1)))
    _run_queue_update(
        conn,
        """
        UPDATE upload_job_queue
        SET status = 'retry_wait',
            available_at = ?,
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            last_error = ?,
            updated_at = ?
        WHERE id = ? AND claimed_by = ? AND status = 'running'
        """,
        (_iso_after(delay_seconds), error_message, utc_now(), job_id, worker_id),
    )


def drain_pending_upload_tasks() -> None:
    """Process queued jobs immediately in test runs."""
    while not _STOP_EVENT.is_set():
        job = claim_next_upload_task("inline")
        if not job:
            return
        _execute_claimed_upload_job(job, "inline")


def recover_upload_tasks() -> None:
    """Restore queued uploads after restart or lease expiry.

    This keeps durable jobs alive after restarts instead of failing them
    immediately. Cancelled jobs are finalized, while expired running jobs are
    moved back to pending so a worker can claim them again.
    """
    now = utc_now()
    with db_session() as conn:
        cancelled_rows = conn.execute(
            """
            SELECT *
            FROM upload_job_queue
            WHERE cancel_requested = 1
              AND status IN ('pending', 'retry_wait')
            ORDER BY created_at ASC
            """
        ).fetchall()
        for row in cancelled_rows:
            job = _row_to_job(row)
            _mark_document_job_cancelled(conn, job.job_type, job.id)
            conn.execute(
                """
                UPDATE upload_job_queue
                SET status = 'cancelled',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (CANCELLED_MESSAGE, now, job.id),
            )

        expired_running_rows = conn.execute(
            """
            SELECT *
            FROM upload_job_queue
            WHERE status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            ORDER BY updated_at ASC
            """,
            (now,),
        ).fetchall()

        for row in expired_running_rows:
            job = _row_to_job(row)
            doc = _get_document_job(conn, job.job_type, job.id)

            if doc and doc["status"] == "done":
                conn.execute(
                    """
                    UPDATE upload_job_queue
                    SET status = 'done',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        last_error = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, job.id),
                )
                continue

            if doc and doc["status"] == "failed":
                error_message = doc["error_message"]
                if error_message == CANCELLED_MESSAGE:
                    conn.execute(
                        """
                        UPDATE upload_job_queue
                        SET status = 'cancelled',
                            claimed_by = NULL,
                            claimed_at = NULL,
                            heartbeat_at = NULL,
                            lease_expires_at = NULL,
                            last_error = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (CANCELLED_MESSAGE, now, job.id),
                    )
                    continue

                if _retryable_error(error_message) and job.attempt_count < job.max_attempts:
                    _reset_document_job_for_retry(conn, job.job_type, job.id)
                    conn.execute(
                        """
                        UPDATE upload_job_queue
                        SET status = 'pending',
                            available_at = ?,
                            claimed_by = NULL,
                            claimed_at = NULL,
                            heartbeat_at = NULL,
                            lease_expires_at = NULL,
                            last_error = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, error_message, now, job.id),
                    )
                    continue

                conn.execute(
                    """
                    UPDATE upload_job_queue
                    SET status = 'failed',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (error_message or INTERRUPTED_MESSAGE, now, job.id),
                )
                continue

            if job.attempt_count < job.max_attempts:
                conn.execute(
                    """
                    UPDATE upload_job_queue
                    SET status = 'pending',
                        available_at = ?,
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, INTERRUPTED_MESSAGE, now, job.id),
                )
            else:
                _mark_document_job_failed(conn, job.job_type, job.id, INTERRUPTED_MESSAGE)
                conn.execute(
                    """
                    UPDATE upload_job_queue
                    SET status = 'failed',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (INTERRUPTED_MESSAGE, now, job.id),
                )


def _finalize_queue_from_document_state(job: QueuedUploadJob, worker_id: str) -> None:
    with db_session() as conn:
        doc = _get_document_job(conn, job.job_type, job.id)
        if not doc:
            _mark_queue_failed(job.id, worker_id, "Upload job not found", conn=conn)
            return

        status = doc["status"]
        error_message = doc["error_message"]

        if status == "done":
            _mark_queue_done(job.id, worker_id, conn=conn)
            return

        if status == "failed" and error_message == CANCELLED_MESSAGE:
            _mark_queue_cancelled(job.id, worker_id, conn=conn)
            return

        if status == "failed" and _retryable_error(error_message) and job.attempt_count < job.max_attempts:
            _reset_document_job_for_retry(conn, job.job_type, job.id)
            _mark_queue_retry_wait(job.id, worker_id, error_message or INTERRUPTED_MESSAGE, job.attempt_count, conn=conn)
            return

        if status == "failed":
            _mark_queue_failed(job.id, worker_id, error_message or INTERRUPTED_MESSAGE, conn=conn)
            return

        if job.attempt_count < job.max_attempts:
            _mark_queue_retry_wait(job.id, worker_id, INTERRUPTED_MESSAGE, job.attempt_count, conn=conn)
            return

        _mark_document_job_failed(conn, job.job_type, job.id, INTERRUPTED_MESSAGE)
        _mark_queue_failed(job.id, worker_id, INTERRUPTED_MESSAGE, conn=conn)


def _worker_loop(worker_id: str) -> None:
    while not _STOP_EVENT.is_set():
        job = claim_next_upload_task(worker_id)
        if not job:
            sleep(0.5)
            continue

        _execute_claimed_upload_job(job, worker_id)


def _recovery_loop() -> None:
    while not _STOP_EVENT.wait(RECOVERY_SECONDS):
        recover_upload_tasks()


def start_upload_worker(worker_count: int | None = None) -> None:
    global _RECOVERY_THREAD
    if _WORKER_THREADS and all(thread.is_alive() for thread in _WORKER_THREADS):
        return

    _STOP_EVENT.clear()
    recover_upload_tasks()

    count = worker_count or DEFAULT_WORKER_COUNT
    count = max(1, min(count, MAX_WORKER_COUNT))

    _WORKER_THREADS.clear()
    for index in range(count):
        worker_id = f"worker-{index + 1}"
        thread = Thread(
            target=_worker_loop,
            args=(worker_id,),
            name=f"upload-worker-{index + 1}",
            daemon=True,
        )
        _WORKER_THREADS.append(thread)
        thread.start()

    _RECOVERY_THREAD = Thread(target=_recovery_loop, name="upload-recovery", daemon=True)
    _RECOVERY_THREAD.start()


def stop_upload_worker() -> None:
    global _RECOVERY_THREAD
    _STOP_EVENT.set()
    for thread in _WORKER_THREADS:
        if thread.is_alive():
            thread.join(timeout=5)
    _WORKER_THREADS.clear()
    if _RECOVERY_THREAD and _RECOVERY_THREAD.is_alive():
        _RECOVERY_THREAD.join(timeout=5)
    _RECOVERY_THREAD = None
