from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from uuid import uuid4

from app.config import get_settings
from app.services.document_core import delete_document_core, insert_document_core
from app.services.credit_memo_extractor import (
    CreditMemoExtraction,
    extract_credit_memo,
    normalize_ref,
)
from app.services.pdf_assembler import assemble_pdf
from app.services.pdf_splitter import SplitPdfPage, split_pdf_pages
from app.services.storage import write_credit_memo_assembled, write_credit_memo_page


@dataclass(slots=True)
class ImportResult:
    total_pages: int
    credit_memo_count: int
    credit_memo_numbers: list[str]
    warnings: list[str]


@dataclass(slots=True)
class StagedPage:
    split_page: SplitPdfPage
    extraction: CreditMemoExtraction


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_credit_memo_date_for_storage(value: str) -> str:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    try:
        day, month, year = value.split("/")
        if len(year) == 2:
            return datetime(int(year) + 2000, int(month), int(day)).date().isoformat()
    except ValueError:
        pass

    raise ValueError(f"Unsupported Credit Memo Date format: {value}")


def stage_credit_memo_pdf(
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> tuple[list[StagedPage], list[str]]:
    pages = split_pdf_pages(pdf_bytes, on_page=on_page)
    staged: list[StagedPage] = []
    warnings: list[str] = []

    if not pages:
        raise ValueError("PDF has no pages")

    for page in pages:
        extraction = extract_credit_memo(page.raw_text)
        if not extraction:
            raise ValueError(f"Cannot extract Credit Memo No. from page {page.page_number}")
        staged.append(StagedPage(split_page=page, extraction=extraction))

    return staged, warnings


def import_credit_memo_pdf(
    conn: Connection,
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> ImportResult:
    """Credit Memo-scoped replace import.

    Split and extract are completed before any database replace occurs. For
    every Credit Memo No. present in this upload, old rows are deleted and
    replaced with freshly grouped pages, refs, and assembled PDF cache.
    """
    settings = get_settings()
    staged, warnings = stage_credit_memo_pdf(pdf_bytes, on_page=on_page)

    grouped: dict[str, list[StagedPage]] = defaultdict(list)
    for item in staged:
        grouped[item.extraction.credit_memo_number].append(item)

    now = utc_now()
    credit_memo_numbers = sorted(grouped)

    for credit_memo_number, items in grouped.items():
        items = sorted(items, key=lambda item: item.split_page.page_number)
        first = items[0].extraction
        date_values = {
            normalize_credit_memo_date_for_storage(item.extraction.credit_memo_date)
            for item in items
            if item.extraction.credit_memo_date
        }
        if len(date_values) > 1:
            warnings.append(
                f"Credit Memo {credit_memo_number} has multiple credit memo dates: {sorted(date_values)}"
            )
        credit_memo_date = sorted(date_values)[0] if date_values else None

        assembled_bytes = assemble_pdf([item.split_page.pdf_bytes for item in items])

        assembled_pdf_path = None
        if settings.pdf_storage_mode != "binary":
            assembled_pdf_path = write_credit_memo_assembled(credit_memo_number, assembled_bytes)

        merged_refs: dict[str, set[str]] = defaultdict(set)
        for page_order, item in enumerate(items, start=1):
            for ref_type, values in item.extraction.refs.items():
                merged_refs[ref_type].update(values)

        delete_document_core(conn, "credit-memos", credit_memo_number)
        insert_document_core(
            conn,
            doc_type="credit-memos",
            root_key=credit_memo_number,
            display_key=credit_memo_number,
            root_date=credit_memo_date,
            name=first.name,
            customer_code=first.customer_code,
            storage_mode=settings.pdf_storage_mode,
            assembled_pdf=assembled_bytes if settings.pdf_storage_mode == "binary" else None,
            assembled_pdf_path=assembled_pdf_path,
            assembled_page_count=len(items),
            page_rows=[
                (
                    page_order,
                    item.split_page.pdf_bytes if settings.pdf_storage_mode == "binary" else None,
                    write_credit_memo_page(credit_memo_number, page_order, item.split_page.pdf_bytes)
                    if settings.pdf_storage_mode != "binary"
                    else None,
                    item.split_page.raw_text,
                )
                for page_order, item in enumerate(items, start=1)
            ],
            refs=merged_refs,
        )

    return ImportResult(
        total_pages=len(staged),
        credit_memo_count=len(grouped),
        credit_memo_numbers=credit_memo_numbers,
        warnings=warnings,
    )
