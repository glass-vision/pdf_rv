from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any

from app.config import get_settings
from app.services.document_core import delete_document_core, insert_document_core
from app.services.pdf_assembler import assemble_pdf
from app.services.pdf_splitter import SplitPdfPage, split_pdf_pages


@dataclass(slots=True)
class StatementImportResult:
    total_pages: int
    statement_count: int
    statement_keys: list[str]
    warnings: list[str]


@dataclass(slots=True)
class StagedStatementPage:
    split_page: SplitPdfPage
    extraction: Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_statement_pdf(
    conn: Connection,
    pdf_bytes: bytes,
    *,
    extractor: Callable[[str], Any | None],
    key_attr: str,
    assembled_writer: Callable[[str, bytes], str],
    page_writer: Callable[[str, int, bytes], str],
    normalizer: Callable[[str], str],
    shared_doc_type: str,
    on_page: Callable[[int], None] | None = None,
    # legacy params kept for call-site compat — ignored
    table: str = "",
    key_column: str = "",
    pages_table: str = "",
    refs_table: str = "",
    foreign_key: str = "",
    parent_columns: list[str] | None = None,
) -> StatementImportResult:
    pages = split_pdf_pages(pdf_bytes, on_page=on_page)
    if not pages:
        raise ValueError("PDF has no pages")

    staged: list[StagedStatementPage] = []
    for page in pages:
        extraction = extractor(page.raw_text)
        if not extraction:
            raise ValueError(f"Cannot extract statement key from page {page.page_number}")
        staged.append(StagedStatementPage(page, extraction))

    grouped: dict[str, list[StagedStatementPage]] = defaultdict(list)
    for item in staged:
        grouped[getattr(item.extraction, key_attr)].append(item)

    settings = get_settings()
    now = utc_now()
    statement_keys = sorted(grouped)

    for key, items in grouped.items():
        items.sort(key=lambda item: item.split_page.page_number)
        first = items[0].extraction
        assembled_bytes = assemble_pdf([item.split_page.pdf_bytes for item in items])
        assembled_pdf = assembled_bytes if settings.pdf_storage_mode == "binary" else None
        assembled_path = (
            assembled_writer(key, assembled_bytes) if settings.pdf_storage_mode == "file" else None
        )

        merged_refs: dict[str, set[str]] = defaultdict(set)
        page_ref_rows: list[tuple[int, dict[str, set[str]]]] = []
        for page_order, item in enumerate(items, start=1):
            for ref_type, values_for_type in item.extraction.refs.items():
                merged_refs[ref_type].update(values_for_type)
            page_ref_rows.append(
                (
                    page_order,
                    {
                        ref_type: set(values_for_type)
                        for ref_type, values_for_type in item.extraction.refs.items()
                        if values_for_type
                    },
                )
            )

        period_from = getattr(first, "period_from", None)
        period_to = getattr(first, "period_to", None)
        root_date = period_to or getattr(first, "statement_date", None) or period_from
        account_number = getattr(first, "account_number", None)

        delete_document_core(conn, shared_doc_type, key)
        insert_document_core(
            conn,
            doc_type=shared_doc_type,
            root_key=key,
            display_key=key,
            root_date=root_date,
            period_from=period_from,
            name=getattr(first, "account_name", None),
            customer_code=account_number,
            storage_mode=settings.pdf_storage_mode,
            assembled_pdf=assembled_pdf,
            assembled_pdf_path=assembled_path,
            assembled_page_count=len(items),
            page_rows=[
                (
                    page_order,
                    item.split_page.pdf_bytes if settings.pdf_storage_mode == "binary" else None,
                    page_writer(key, page_order, item.split_page.pdf_bytes)
                    if settings.pdf_storage_mode == "file"
                    else None,
                    item.split_page.raw_text,
                )
                for page_order, item in enumerate(items, start=1)
            ],
            refs=merged_refs,
            page_ref_rows=page_ref_rows,
        )

        conn.commit()

    return StatementImportResult(len(staged), len(grouped), statement_keys, [])
