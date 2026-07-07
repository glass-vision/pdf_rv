from sqlite3 import Connection
from collections.abc import Callable

from app.services.pdf_assembler import assemble_pdf
from app.services.pdf_splitter import split_pdf_pages
from app.services.statement_importer import StatementImportResult, utc_now
from app.services.storage import write_uob_ca_statement_assembled, write_uob_ca_statement_page
from app.services.uob_ca_statement_extractor import (
    UobCaStatementExtraction,
    UobCaStatementHeader,
    derive_statement_key,
    extract_uob_ca_statement,
    extract_uob_ca_statement_header,
    extract_uob_ca_statement_refs,
    extract_uob_ca_statement_transactions,
    normalize_ref,
)
from app.config import get_settings
from app.services.document_core import delete_document_core, insert_document_core
from uuid import uuid4


def import_uob_ca_statement_pdf(
    conn: Connection, pdf_bytes: bytes, on_page: Callable[[int], None] | None = None
) -> StatementImportResult:
    pages = split_pdf_pages(pdf_bytes, on_page=on_page)
    if not pages:
        raise ValueError("PDF has no pages")

    staged: list[tuple[int, bytes, str, UobCaStatementHeader, UobCaStatementExtraction | None]] = []
    full_ctx: UobCaStatementExtraction | None = None
    for page in pages:
        header = extract_uob_ca_statement_header(page.raw_text)
        extraction = extract_uob_ca_statement(page.raw_text)
        staged.append((page.page_number, page.pdf_bytes, page.raw_text, header, extraction))
        if extraction and not full_ctx:
            full_ctx = extraction

    def merge_header(target: UobCaStatementHeader | None, source: UobCaStatementHeader) -> UobCaStatementHeader:
        if target is None:
            return source
        for field in (
            "statement_date",
            "company_id",
            "account_number",
            "account_name",
            "account_type",
            "account_currency",
            "account_branch",
            "period_from",
            "period_to",
        ):
            current = getattr(target, field)
            incoming = getattr(source, field)
            if current is None and incoming is not None:
                setattr(target, field, incoming)
        return target

    merged_header: UobCaStatementHeader | None = None
    for _, _, _, header, _ in staged:
        merged_header = merge_header(merged_header, header)

    if full_ctx:
        ctx = full_ctx
    else:
        if not merged_header or not merged_header.account_number or not merged_header.period_from or not merged_header.period_to:
            raise ValueError("Cannot extract statement key from page 1")
        ctx = UobCaStatementExtraction(
            statement_key=derive_statement_key(
                merged_header.account_number,
                merged_header.period_from,
                merged_header.period_to,
            ),
            statement_date=merged_header.statement_date,
            period_from=merged_header.period_from,
            period_to=merged_header.period_to,
            company_id=merged_header.company_id,
            account_number=merged_header.account_number,
            account_name=merged_header.account_name,
            account_type=merged_header.account_type,
            account_currency=merged_header.account_currency,
            account_branch=merged_header.account_branch,
            refs={},
        )

    settings = get_settings()
    now = utc_now()

    resolved: list[tuple[int, bytes, str, UobCaStatementExtraction]] = []
    for page_number, page_bytes, raw_text, header, extraction in staged:
        if extraction:
            if extraction.statement_key != ctx.statement_key:
                raise ValueError(f"Cannot extract statement key from page {page_number}")
            resolved.append((page_number, page_bytes, raw_text, extraction))
            continue

        if header.account_number and normalize_ref(header.account_number) != normalize_ref(ctx.account_number or ""):
            raise ValueError(f"Cannot extract statement key from page {page_number}")
        if header.period_from and header.period_to:
            page_key = derive_statement_key(header.account_number or ctx.account_number or "", header.period_from, header.period_to)
            if page_key != ctx.statement_key:
                raise ValueError(f"Cannot extract statement key from page {page_number}")

        resolved.append(
            (
                page_number,
                page_bytes,
                raw_text,
                UobCaStatementExtraction(
                    statement_key=ctx.statement_key,
                    statement_date=ctx.statement_date,
                    period_from=ctx.period_from,
                    period_to=ctx.period_to,
                    company_id=ctx.company_id,
                    account_number=ctx.account_number,
                    account_name=ctx.account_name,
                    account_type=ctx.account_type,
                    account_currency=ctx.account_currency,
                    account_branch=ctx.account_branch,
                    refs=extract_uob_ca_statement_refs(
                        raw_text, ctx.account_number or "", ctx.company_id,
                        ctx.account_name, ctx.statement_date, ctx.period_to,
                    ),
                ),
            )
        )

    key = ctx.statement_key
    assembled_bytes = assemble_pdf([page_bytes for _, page_bytes, _, _ in sorted(resolved)])
    assembled_pdf = assembled_bytes if settings.pdf_storage_mode == "binary" else None
    assembled_path = (
        write_uob_ca_statement_assembled(key, assembled_bytes) if settings.pdf_storage_mode == "file" else None
    )

    merged_refs: dict[str, set[str]] = {}
    page_ref_rows: list[tuple[int, dict[str, set[str]]]] = []
    for page_order, (_, _, _, extraction) in enumerate(sorted(resolved), start=1):
        for ref_type, values_for_type in extraction.refs.items():
            merged_refs.setdefault(ref_type, set()).update(values_for_type)
        page_ref_rows.append(
            (
                page_order,
                {
                    ref_type: set(values_for_type)
                    for ref_type, values_for_type in extraction.refs.items()
                    if values_for_type
                },
            )
        )

    delete_document_core(conn, "uob-ca-statements", key)
    root_id = insert_document_core(
        conn,
        doc_type="uob-ca-statements",
        root_key=key,
        display_key=key,
        root_date=ctx.period_to or ctx.statement_date,
        period_from=ctx.period_from,
        name=ctx.account_name,
        customer_code=ctx.account_number,
        storage_mode=settings.pdf_storage_mode,
        assembled_pdf=assembled_pdf,
        assembled_pdf_path=assembled_path,
        assembled_page_count=len(resolved),
        page_rows=[
            (
                page_order,
                page_bytes if settings.pdf_storage_mode == "binary" else None,
                write_uob_ca_statement_page(key, page_order, page_bytes)
                if settings.pdf_storage_mode == "file"
                else None,
                raw_text,
            )
            for page_order, (_, page_bytes, raw_text, _) in enumerate(sorted(resolved), start=1)
        ],
        refs=merged_refs,
        page_ref_rows=page_ref_rows,
    )

    for page_order, (_, _, raw_text, _) in enumerate(sorted(resolved), start=1):
        for transaction in extract_uob_ca_statement_transactions(raw_text):
            conn.execute(
                """
                INSERT INTO uob_ca_statement_transactions (
                    id, document_root_id, page_order, row_order, transaction_date,
                    value_date, posting_date, transaction_time, transaction_type,
                    description, transaction_id, transaction_ref, customer_ref,
                    customer_code, deposit_raw, deposit, withdrawal_raw, withdrawal,
                    amount_raw, amount, balance_raw, balance, source_line, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    root_id,
                    page_order,
                    transaction.row_order,
                    transaction.transaction_date,
                    transaction.value_date,
                    transaction.posting_date,
                    transaction.transaction_time,
                    transaction.transaction_type,
                    transaction.description,
                    transaction.transaction_id,
                    transaction.transaction_ref,
                    transaction.customer_ref,
                    transaction.customer_code,
                    transaction.deposit_raw,
                    transaction.deposit,
                    transaction.withdrawal_raw,
                    transaction.withdrawal,
                    transaction.amount_raw,
                    transaction.amount,
                    transaction.balance_raw,
                    transaction.balance,
                    transaction.source_line,
                    now,
                ),
            )

    return StatementImportResult(len(resolved), 1, [ctx.statement_key], [])
