from sqlite3 import Connection
from collections.abc import Callable
from uuid import uuid4

from app.services.kbank_statement_extractor import extract_kbank_statement, normalize_ref
from app.services.statement_importer import StatementImportResult, import_statement_pdf, utc_now
from app.services.storage import write_kbank_statement_assembled, write_kbank_statement_page


def refresh_kbank_check_rows(conn: Connection, statement_reference: str) -> None:
    now = utc_now()
    root = conn.execute(
        "SELECT id FROM document_roots WHERE doc_type = 'kbank-statements' AND root_key = ?",
        (statement_reference,),
    ).fetchone()
    if not root:
        return
    root_id = root["id"]
    conn.execute("DELETE FROM kbank_check_rows WHERE document_root_id = ?", (root_id,))
    page_rows = conn.execute(
        """
        SELECT page_order, raw_text
        FROM document_pages
        WHERE document_root_id = ?
        ORDER BY page_order
        """,
        (root_id,),
    ).fetchall()
    for page_row in page_rows:
        extraction = extract_kbank_statement(str(page_row["raw_text"] or ""))
        if not extraction:
            continue
        for row_order, check_row in enumerate(extraction.check_rows, start=1):
            conn.execute(
                """
                INSERT INTO kbank_check_rows (
                    id, document_root_id, page_order, row_order, event_type, check_no,
                    txn_date, amount_raw, amount, balance_raw, balance, bank_hint,
                    source_line, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    root_id,
                    page_row["page_order"],
                    row_order,
                    check_row["event_type"],
                    check_row["check_no"],
                    check_row.get("txn_date"),
                    check_row["amount_raw"],
                    check_row["amount"],
                    check_row.get("balance_raw"),
                    check_row.get("balance"),
                    check_row.get("bank_hint"),
                    check_row["source_line"],
                    now,
                ),
            )


def import_kbank_statement_pdf(
    conn: Connection, pdf_bytes: bytes, on_page: Callable[[int], None] | None = None
) -> StatementImportResult:
    result = import_statement_pdf(
        conn, pdf_bytes, extractor=extract_kbank_statement,
        key_attr="statement_reference", table="kbank_statements",
        key_column="statement_reference", pages_table="kbank_statement_pages",
        refs_table="kbank_statement_refs", foreign_key="kbank_statement_id",
        parent_columns=["period_from", "period_to", "account_number", "account_name", "branch_name"],
        page_writer=write_kbank_statement_page,
        assembled_writer=write_kbank_statement_assembled,
        normalizer=normalize_ref,
        shared_doc_type="kbank-statements",
        on_page=on_page,
    )
    for statement_reference in result.statement_keys:
        refresh_kbank_check_rows(conn, statement_reference)
    conn.commit()
    return result
