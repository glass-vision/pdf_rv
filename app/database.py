from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator

from app.config import ensure_data_dirs, get_settings


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS document_roots (
    id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    root_key TEXT NOT NULL,
    display_key TEXT NOT NULL,
    root_date TEXT,
    period_from TEXT,
    name TEXT,
    customer_code TEXT,

    assembled_storage_mode TEXT NOT NULL,
    assembled_pdf BLOB,
    assembled_pdf_path TEXT,
    assembled_page_count INTEGER NOT NULL DEFAULT 0,
    assembled_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    CHECK (root_date IS NULL OR root_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (period_from IS NULL OR period_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (
        (assembled_storage_mode = 'binary' AND assembled_pdf IS NOT NULL AND assembled_pdf_path IS NULL)
        OR
        (assembled_storage_mode = 'file' AND assembled_pdf IS NULL AND assembled_pdf_path IS NOT NULL)
    ),

    UNIQUE(doc_type, root_key)
);

CREATE TABLE IF NOT EXISTS document_pages (
    id TEXT PRIMARY KEY,
    document_root_id TEXT NOT NULL,
    page_order INTEGER NOT NULL,

    storage_mode TEXT NOT NULL,
    page_pdf BLOB,
    page_pdf_path TEXT,

    raw_text TEXT,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_root_id)
        REFERENCES document_roots(id)
        ON DELETE CASCADE,

    UNIQUE(document_root_id, page_order),

    CHECK (
        (storage_mode = 'binary' AND page_pdf IS NOT NULL AND page_pdf_path IS NULL)
        OR
        (storage_mode = 'file' AND page_pdf IS NULL AND page_pdf_path IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS document_page_refs (
    id TEXT PRIMARY KEY,
    document_page_id TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_page_id)
        REFERENCES document_pages(id)
        ON DELETE CASCADE,

    UNIQUE(document_page_id, ref_type, normalized_value)
);

CREATE TABLE IF NOT EXISTS document_refs (
    id TEXT PRIMARY KEY,
    document_root_id TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_root_id)
        REFERENCES document_roots(id)
        ON DELETE CASCADE,

    UNIQUE(document_root_id, ref_type, normalized_value)
);

CREATE TABLE IF NOT EXISTS receive_voucher_bank_rows (
    id TEXT PRIMARY KEY,
    document_root_id TEXT NOT NULL,
    row_order INTEGER NOT NULL,
    bank_code TEXT,
    bank_account_no TEXT,
    check_no TEXT,
    bill_no TEXT,
    currency_amt_raw TEXT,
    currency_amt TEXT,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_root_id)
        REFERENCES document_roots(id)
        ON DELETE CASCADE,

    UNIQUE(document_root_id, row_order)
);

CREATE TABLE IF NOT EXISTS kbank_check_rows (
    id TEXT PRIMARY KEY,
    document_root_id TEXT NOT NULL,
    page_order INTEGER NOT NULL,
    row_order INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    check_no TEXT NOT NULL,
    txn_date TEXT,
    amount_raw TEXT NOT NULL,
    amount TEXT NOT NULL,
    balance_raw TEXT,
    balance TEXT,
    bank_hint TEXT,
    source_line TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_root_id)
        REFERENCES document_roots(id)
        ON DELETE CASCADE,

    UNIQUE(document_root_id, page_order, row_order)
);

CREATE TABLE IF NOT EXISTS uob_ca_statement_transactions (
    id TEXT PRIMARY KEY,
    document_root_id TEXT NOT NULL,
    page_order INTEGER NOT NULL,
    row_order INTEGER NOT NULL,
    transaction_date TEXT,
    value_date TEXT,
    posting_date TEXT,
    transaction_time TEXT,
    transaction_type TEXT,
    description TEXT,
    transaction_id TEXT,
    transaction_ref TEXT,
    customer_ref TEXT,
    customer_code TEXT,
    deposit_raw TEXT,
    deposit TEXT,
    withdrawal_raw TEXT,
    withdrawal TEXT,
    amount_raw TEXT NOT NULL,
    amount TEXT NOT NULL,
    balance_raw TEXT,
    balance TEXT,
    source_line TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (document_root_id)
        REFERENCES document_roots(id)
        ON DELETE CASCADE,

    UNIQUE(document_root_id, page_order, row_order)
);

CREATE INDEX IF NOT EXISTS idx_document_roots_lookup
ON document_roots(doc_type, root_key, root_date);

CREATE INDEX IF NOT EXISTS idx_document_roots_recent
ON document_roots(doc_type, updated_at DESC, root_key DESC);

CREATE INDEX IF NOT EXISTS idx_document_roots_period
ON document_roots(doc_type, period_from, root_date);

CREATE INDEX IF NOT EXISTS idx_document_pages_root_order
ON document_pages(document_root_id, page_order);

CREATE INDEX IF NOT EXISTS idx_document_page_refs_lookup
ON document_page_refs(ref_type, normalized_value);

CREATE INDEX IF NOT EXISTS idx_document_page_refs_page_order
ON document_page_refs(document_page_id, created_at, ref_value);

CREATE INDEX IF NOT EXISTS idx_document_refs_lookup
ON document_refs(ref_type, normalized_value);

CREATE INDEX IF NOT EXISTS idx_document_refs_root_order
ON document_refs(document_root_id, created_at, ref_value);

CREATE INDEX IF NOT EXISTS idx_receive_voucher_bank_rows_root_order
ON receive_voucher_bank_rows(document_root_id, row_order);

CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_root_order
ON kbank_check_rows(document_root_id, page_order, row_order);

CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_check
ON kbank_check_rows(check_no, event_type, amount);

CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_amount
ON kbank_check_rows(event_type, amount);

CREATE INDEX IF NOT EXISTS idx_uob_ca_statement_transactions_match
ON uob_ca_statement_transactions(document_root_id, amount);

CREATE TABLE IF NOT EXISTS rv_uob_confirmations (
    id TEXT PRIMARY KEY,
    rv_bank_row_id TEXT NOT NULL,
    uob_statement_key TEXT NOT NULL,
    uob_page_order INTEGER NOT NULL,
    uob_row_order INTEGER NOT NULL,
    match_rule TEXT,
    match_conditions TEXT,
    selection_source TEXT,
    confirmed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (rv_bank_row_id)
        REFERENCES receive_voucher_bank_rows(id)
        ON DELETE CASCADE,

    UNIQUE(rv_bank_row_id)
);

CREATE INDEX IF NOT EXISTS idx_rv_uob_confirmations_statement_row
ON rv_uob_confirmations(uob_statement_key, uob_page_order, uob_row_order);

CREATE TABLE IF NOT EXISTS rv_uob_auto_confirm_suppressions (
    rv_bank_row_id TEXT PRIMARY KEY,
    suppressed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (rv_bank_row_id)
        REFERENCES receive_voucher_bank_rows(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rv_kbank_confirmations (
    id TEXT PRIMARY KEY,
    rv_bank_row_id TEXT NOT NULL,
    kbank_statement_key TEXT NOT NULL,
    kbank_page_order INTEGER NOT NULL,
    kbank_row_order INTEGER NOT NULL,
    match_rule TEXT,
    match_conditions TEXT,
    selection_source TEXT,
    confirmed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (rv_bank_row_id)
        REFERENCES receive_voucher_bank_rows(id)
        ON DELETE CASCADE,

    UNIQUE(rv_bank_row_id)
);

CREATE INDEX IF NOT EXISTS idx_rv_kbank_confirmations_statement_row
ON rv_kbank_confirmations(kbank_statement_key, kbank_page_order, kbank_row_order);

CREATE TABLE IF NOT EXISTS rv_kbank_auto_confirm_suppressions (
    rv_bank_row_id TEXT PRIMARY KEY,
    suppressed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (rv_bank_row_id)
        REFERENCES receive_voucher_bank_rows(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS upload_jobs (
    id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',

    total_pages INTEGER,
    processed_pages INTEGER,
    voucher_count INTEGER,
    voucher_numbers TEXT,
    invoice_count INTEGER,
    invoice_numbers TEXT,
    credit_memo_count INTEGER,
    credit_memo_numbers TEXT,
    statement_count INTEGER,
    statement_references TEXT,
    statement_keys TEXT,
    warnings TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    CHECK (
        doc_type IN (
            '',
            'receive-vouchers',
            'invoices',
            'credit-memos',
            'kbank-statements',
            'uob-ca-statements'
        )
    )
);

CREATE TABLE IF NOT EXISTS upload_job_queue (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,

    available_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    CHECK (status IN ('pending', 'running', 'retry_wait', 'done', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_upload_job_queue_claim
ON upload_job_queue(status, available_at, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_upload_job_queue_lease
ON upload_job_queue(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_upload_job_queue_type
ON upload_job_queue(job_type, status);

"""

SQLITE_BUSY_TIMEOUT_MS = 30000
_DB_INIT_LOCK = Lock()


def _connect_raw(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_data_dirs()
    settings = get_settings()
    path = Path(db_path or settings.database_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if settings.effective_sqlite_locking_mode == "exclusive":
        conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};")
    return conn


def _configure_database_file(conn: sqlite3.Connection) -> None:
    if get_settings().effective_sqlite_locking_mode == "exclusive":
        conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        # Keep bootstrapping available by continuing with the existing journal
        # mode instead of failing startup.
        pass


def _has_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'upload_jobs'"
    ).fetchone()
    return row is not None


def _reset_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = path.with_name(path.name + suffix)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _migrate_schema(conn: sqlite3.Connection) -> None:
    # upload_jobs column migrations
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(upload_jobs)")}
    if "doc_type" not in columns:
        conn.execute("ALTER TABLE upload_jobs ADD COLUMN doc_type TEXT NOT NULL DEFAULT ''")
    if "processed_pages" not in columns:
        conn.execute("ALTER TABLE upload_jobs ADD COLUMN processed_pages INTEGER")
    if "cancel_requested" not in columns:
        conn.execute("ALTER TABLE upload_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
    if "client_id" not in columns:
        conn.execute("ALTER TABLE upload_jobs ADD COLUMN client_id TEXT NOT NULL DEFAULT ''")
    for column_sql in (
        "invoice_count INTEGER",
        "invoice_numbers TEXT",
        "credit_memo_count INTEGER",
        "credit_memo_numbers TEXT",
        "statement_count INTEGER",
        "statement_references TEXT",
        "statement_keys TEXT",
    ):
        column_name = column_sql.split()[0]
        if column_name not in columns:
            conn.execute(f"ALTER TABLE upload_jobs ADD COLUMN {column_sql}")

    # document_roots: add period_from column if missing
    dr_columns = {row["name"] for row in conn.execute("PRAGMA table_info(document_roots)")}
    if "period_from" not in dr_columns:
        conn.execute("ALTER TABLE document_roots ADD COLUMN period_from TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_roots_period "
            "ON document_roots(doc_type, period_from, root_date)"
        )

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "receive_voucher_bank_rows" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS receive_voucher_bank_rows (
                id TEXT PRIMARY KEY,
                document_root_id TEXT NOT NULL,
                row_order INTEGER NOT NULL,
                bank_code TEXT,
                bank_account_no TEXT,
                check_no TEXT,
                bill_no TEXT,
                currency_amt_raw TEXT,
                currency_amt TEXT,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_root_id)
                    REFERENCES document_roots(id)
                    ON DELETE CASCADE,

                UNIQUE(document_root_id, row_order)
            );

            CREATE INDEX IF NOT EXISTS idx_receive_voucher_bank_rows_root_order
            ON receive_voucher_bank_rows(document_root_id, row_order);
            """
        )

    if "kbank_check_rows" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kbank_check_rows (
                id TEXT PRIMARY KEY,
                document_root_id TEXT NOT NULL,
                page_order INTEGER NOT NULL,
                row_order INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                check_no TEXT NOT NULL,
                txn_date TEXT,
                amount_raw TEXT NOT NULL,
                amount TEXT NOT NULL,
                balance_raw TEXT,
                balance TEXT,
                bank_hint TEXT,
                source_line TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_root_id)
                    REFERENCES document_roots(id)
                    ON DELETE CASCADE,

                UNIQUE(document_root_id, page_order, row_order)
            );
            CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_root_order
            ON kbank_check_rows(document_root_id, page_order, row_order);
            CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_check
            ON kbank_check_rows(check_no, event_type, amount);
            CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_amount
            ON kbank_check_rows(event_type, amount);
            """
        )
    if "uob_ca_statement_transactions" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS uob_ca_statement_transactions (
                id TEXT PRIMARY KEY,
                document_root_id TEXT NOT NULL,
                page_order INTEGER NOT NULL,
                row_order INTEGER NOT NULL,
                transaction_date TEXT,
                value_date TEXT,
                posting_date TEXT,
                transaction_time TEXT,
                transaction_type TEXT,
                description TEXT,
                transaction_id TEXT,
                transaction_ref TEXT,
                customer_ref TEXT,
                customer_code TEXT,
                deposit_raw TEXT,
                deposit TEXT,
                withdrawal_raw TEXT,
                withdrawal TEXT,
                amount_raw TEXT NOT NULL,
                amount TEXT NOT NULL,
                balance_raw TEXT,
                balance TEXT,
                source_line TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_root_id)
                    REFERENCES document_roots(id)
                    ON DELETE CASCADE,

                UNIQUE(document_root_id, page_order, row_order)
            );

            CREATE INDEX IF NOT EXISTS idx_uob_ca_statement_transactions_match
            ON uob_ca_statement_transactions(document_root_id, amount);
            """
        )
    if "rv_uob_confirmations" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rv_uob_confirmations (
                id TEXT PRIMARY KEY,
                rv_bank_row_id TEXT NOT NULL,
                uob_statement_key TEXT NOT NULL,
                uob_page_order INTEGER NOT NULL,
                uob_row_order INTEGER NOT NULL,
                match_rule TEXT,
                match_conditions TEXT,
                selection_source TEXT,
                confirmed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (rv_bank_row_id)
                    REFERENCES receive_voucher_bank_rows(id)
                    ON DELETE CASCADE,

                UNIQUE(rv_bank_row_id)
            );

            CREATE INDEX IF NOT EXISTS idx_rv_uob_confirmations_statement_row
            ON rv_uob_confirmations(uob_statement_key, uob_page_order, uob_row_order);
            """
        )
    if "rv_uob_auto_confirm_suppressions" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rv_uob_auto_confirm_suppressions (
                rv_bank_row_id TEXT PRIMARY KEY,
                suppressed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (rv_bank_row_id)
                    REFERENCES receive_voucher_bank_rows(id)
                    ON DELETE CASCADE
            );
            """
        )
    if "rv_kbank_confirmations" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rv_kbank_confirmations (
                id TEXT PRIMARY KEY,
                rv_bank_row_id TEXT NOT NULL,
                kbank_statement_key TEXT NOT NULL,
                kbank_page_order INTEGER NOT NULL,
                kbank_row_order INTEGER NOT NULL,
                match_rule TEXT,
                match_conditions TEXT,
                selection_source TEXT,
                confirmed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (rv_bank_row_id)
                    REFERENCES receive_voucher_bank_rows(id)
                    ON DELETE CASCADE,

                UNIQUE(rv_bank_row_id)
            );

            CREATE INDEX IF NOT EXISTS idx_rv_kbank_confirmations_statement_row
            ON rv_kbank_confirmations(kbank_statement_key, kbank_page_order, kbank_row_order);
            """
        )
    if "rv_kbank_auto_confirm_suppressions" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rv_kbank_auto_confirm_suppressions (
                rv_bank_row_id TEXT PRIMARY KEY,
                suppressed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (rv_bank_row_id)
                    REFERENCES receive_voucher_bank_rows(id)
                    ON DELETE CASCADE
            );
            """
        )

    rv_uob_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rv_uob_confirmations)")}
    if "match_rule" not in rv_uob_columns:
        conn.execute("ALTER TABLE rv_uob_confirmations ADD COLUMN match_rule TEXT")
    if "match_conditions" not in rv_uob_columns:
        conn.execute("ALTER TABLE rv_uob_confirmations ADD COLUMN match_conditions TEXT")
    if "selection_source" not in rv_uob_columns:
        conn.execute("ALTER TABLE rv_uob_confirmations ADD COLUMN selection_source TEXT")

    rv_kbank_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rv_kbank_confirmations)")}
    if "match_rule" not in rv_kbank_columns:
        conn.execute("ALTER TABLE rv_kbank_confirmations ADD COLUMN match_rule TEXT")
    if "match_conditions" not in rv_kbank_columns:
        conn.execute("ALTER TABLE rv_kbank_confirmations ADD COLUMN match_conditions TEXT")
    if "selection_source" not in rv_kbank_columns:
        conn.execute("ALTER TABLE rv_kbank_confirmations ADD COLUMN selection_source TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kbank_check_rows_amount ON kbank_check_rows(event_type, amount)"
    )
    if "document_page_refs" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS document_page_refs (
                id TEXT PRIMARY KEY,
                document_page_id TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                ref_value TEXT NOT NULL,
                normalized_value TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (document_page_id)
                    REFERENCES document_pages(id)
                    ON DELETE CASCADE,

                UNIQUE(document_page_id, ref_type, normalized_value)
            );

            CREATE INDEX IF NOT EXISTS idx_document_page_refs_lookup
            ON document_page_refs(ref_type, normalized_value);

            CREATE INDEX IF NOT EXISTS idx_document_page_refs_page_order
            ON document_page_refs(document_page_id, created_at, ref_value);
            """
        )

    # Drop obsolete per-doc tables (children before parents to respect FK order)
    for table in (
        "receive_voucher_pages", "receive_voucher_refs", "receive_vouchers",
        "invoice_pages", "invoice_refs", "invoices",
        "credit_memo_pages", "credit_memo_refs", "credit_memos",
        "kbank_statement_pages", "kbank_statement_refs", "kbank_statements",
        "uob_ca_statement_pages", "uob_ca_statement_refs", "uob_ca_statements",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def init_db(db_path: Path | None = None) -> None:
    path = Path(db_path or get_settings().database_path).expanduser()
    conn = _connect_raw(path)
    try:
        _configure_database_file(conn)
        if _has_schema(conn):
            _migrate_schema(conn)
        else:
            conn.executescript(SCHEMA_SQL)
            _migrate_schema(conn)
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "disk i/o error" not in str(exc).lower():
            raise
        conn.close()
        _reset_database_files(path)
        conn = _connect_raw(path)
        try:
            _configure_database_file(conn)
            conn.executescript(SCHEMA_SQL)
            _migrate_schema(conn)
            conn.commit()
        finally:
            conn.close()
        return
    finally:
        conn.close()


def _ensure_db_initialized(path: Path) -> None:
    with _DB_INIT_LOCK:
        if not path.exists() or path.stat().st_size == 0:
            init_db(path)
            return

        conn = _connect_raw(path)
        try:
            if _has_schema(conn):
                _migrate_schema(conn)
                conn.commit()
                return
        finally:
            conn.close()

        init_db(path)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_data_dirs()
    settings = get_settings()
    path = db_path or settings.database_path
    for attempt in range(2):
        try:
            _ensure_db_initialized(path)
            conn = _connect_raw(path)
            if not _has_schema(conn):
                conn.close()
                _ensure_db_initialized(path)
                conn = _connect_raw(path)
            return conn
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc).lower() or attempt >= 1:
                raise
            ensure_data_dirs()
            path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_db_initialized(path)


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
