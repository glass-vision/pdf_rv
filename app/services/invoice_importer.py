from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlite3 import Connection
from uuid import uuid4

from app.config import get_settings
from app.services.document_core import delete_document_core, insert_document_core
from app.services.pdf_assembler import assemble_pdf
from app.services.pdf_splitter import SplitPdfPage, split_pdf_pages
from app.services.invoice_extractor import (
    InvoiceExtraction,
    extract_invoice,
    normalize_ref,
)
from app.services.storage import write_invoice_assembled, write_invoice_page


@dataclass(slots=True)
class ImportResult:
    total_pages: int
    invoice_count: int
    invoice_numbers: list[str]
    warnings: list[str]


@dataclass(slots=True)
class StagedPage:
    split_page: SplitPdfPage
    extraction: InvoiceExtraction


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_invoice_date_for_storage(value: str) -> str:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported Invoice Date format: {value}")


def stage_invoice_pdf(
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> tuple[list[StagedPage], list[str]]:
    pages = split_pdf_pages(pdf_bytes, on_page=on_page)
    staged: list[StagedPage] = []
    warnings: list[str] = []

    if not pages:
        raise ValueError("PDF has no pages")

    for page in pages:
        extraction = extract_invoice(page.raw_text)
        if not extraction:
            raise ValueError(f"Cannot extract Invoice No. from page {page.page_number}")
        staged.append(StagedPage(split_page=page, extraction=extraction))

    return staged, warnings


def import_invoice_pdf(
    conn: Connection,
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> ImportResult:
    """Invoice-scoped replace import.

    Split and extract are completed before any database replace occurs.
    For every Invoice No. present in this upload, old rows are deleted and
    replaced with freshly grouped pages, refs, and assembled PDF cache.
    """
    settings = get_settings()
    staged, warnings = stage_invoice_pdf(pdf_bytes, on_page=on_page)

    grouped: dict[str, list[StagedPage]] = defaultdict(list)
    for item in staged:
        grouped[item.extraction.invoice_number].append(item)

    now = utc_now()
    invoice_numbers = sorted(grouped)

    for invoice_number, items in grouped.items():
        items = sorted(items, key=lambda item: item.split_page.page_number)
        first = items[0].extraction
        date_values = {
            normalize_invoice_date_for_storage(item.extraction.invoice_date)
            for item in items
            if item.extraction.invoice_date
        }
        if len(date_values) > 1:
            warnings.append(f"Invoice {invoice_number} has multiple invoice dates: {sorted(date_values)}")
        invoice_date = sorted(date_values)[0] if date_values else None

        assembled_bytes = assemble_pdf([item.split_page.pdf_bytes for item in items])

        invoice_id = str(uuid4())
        assembled_pdf = None
        assembled_pdf_path = None
        if settings.pdf_storage_mode == "binary":
            assembled_pdf = assembled_bytes
        else:
            assembled_pdf_path = write_invoice_assembled(invoice_number, assembled_bytes)

        merged_refs: dict[str, set[str]] = defaultdict(set)
        for page_order, item in enumerate(items, start=1):
            for ref_type, values in item.extraction.refs.items():
                merged_refs[ref_type].update(values)

        delete_document_core(conn, "invoices", invoice_number)
        insert_document_core(
            conn,
            doc_type="invoices",
            root_key=invoice_number,
            display_key=invoice_number,
            root_date=invoice_date,
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
                    write_invoice_page(invoice_number, page_order, item.split_page.pdf_bytes)
                    if settings.pdf_storage_mode != "binary"
                    else None,
                    item.split_page.raw_text,
                )
                for page_order, item in enumerate(items, start=1)
            ],
            refs=merged_refs,
        )

        conn.commit()

    return ImportResult(
        total_pages=len(staged),
        invoice_count=len(grouped),
        invoice_numbers=invoice_numbers,
        warnings=warnings,
    )
