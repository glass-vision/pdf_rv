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
from app.services.receive_voucher_extractor import (
    ReceiveVoucherExtraction,
    extract_receive_voucher,
    normalize_ref,
)
from app.services.storage import write_receive_voucher_assembled, write_receive_voucher_page




@dataclass(slots=True)
class ImportResult:
    total_pages: int
    voucher_count: int
    voucher_numbers: list[str]
    warnings: list[str]


@dataclass(slots=True)
class StagedPage:
    split_page: SplitPdfPage
    extraction: ReceiveVoucherExtraction


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_voucher_date_for_storage(value: str) -> str:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported Voucher Date format: {value}")


def stage_receive_voucher_pdf(
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> tuple[list[StagedPage], list[str]]:
    pages = split_pdf_pages(pdf_bytes, on_page=on_page)
    staged: list[StagedPage] = []
    warnings: list[str] = []

    if not pages:
        raise ValueError("PDF has no pages")

    for page in pages:
        extraction = extract_receive_voucher(page.raw_text)
        if not extraction:
            raise ValueError(f"Cannot extract Voucher Number from page {page.page_number}")
        staged.append(StagedPage(split_page=page, extraction=extraction))

    return staged, warnings


def import_receive_voucher_pdf(
    conn: Connection,
    pdf_bytes: bytes,
    on_page: Callable[[int], None] | None = None,
) -> ImportResult:
    """Voucher-scoped replace import.

    Split and extract are completed before any database replace occurs.
    For every Voucher Number present in this upload, old rows are deleted and
    replaced with freshly grouped pages, refs, and assembled PDF cache.
    """
    settings = get_settings()
    staged, warnings = stage_receive_voucher_pdf(pdf_bytes, on_page=on_page)

    grouped: dict[str, list[StagedPage]] = defaultdict(list)
    for item in staged:
        grouped[item.extraction.voucher_number].append(item)

    now = utc_now()
    voucher_numbers = sorted(grouped)

    for voucher_number, items in grouped.items():
        # Keep document order according to the original upload.
        items = sorted(items, key=lambda item: item.split_page.page_number)
        first = items[0].extraction
        date_values = {
            normalize_voucher_date_for_storage(item.extraction.voucher_date)
            for item in items
            if item.extraction.voucher_date
        }
        if len(date_values) > 1:
            warnings.append(f"Voucher {voucher_number} has multiple voucher dates: {sorted(date_values)}")
        voucher_date = sorted(date_values)[0] if date_values else None

        assembled_bytes = assemble_pdf([item.split_page.pdf_bytes for item in items])

        voucher_id = str(uuid4())
        assembled_pdf = None
        assembled_pdf_path = None
        if settings.pdf_storage_mode == "binary":
            assembled_pdf = assembled_bytes
        else:
            assembled_pdf_path = write_receive_voucher_assembled(voucher_number, assembled_bytes)

        merged_refs: dict[str, set[str]] = defaultdict(set)
        merged_bank_rows: list[dict[str, str]] = []
        for page_order, item in enumerate(items, start=1):
            for ref_type, values in item.extraction.refs.items():
                merged_refs[ref_type].update(values)
            for row in item.extraction.bank_rows:
                if row not in merged_bank_rows:
                    merged_bank_rows.append(row)

        delete_document_core(conn, "receive-vouchers", voucher_number)
        root_id = insert_document_core(
            conn,
            doc_type="receive-vouchers",
            root_key=voucher_number,
            display_key=voucher_number,
            root_date=voucher_date,
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
                    write_receive_voucher_page(voucher_number, page_order, item.split_page.pdf_bytes)
                    if settings.pdf_storage_mode != "binary"
                    else None,
                    item.split_page.raw_text,
                )
                for page_order, item in enumerate(items, start=1)
            ],
            refs=merged_refs,
        )

        for row_order, bank_row in enumerate(merged_bank_rows, start=1):
            conn.execute(
                """
                INSERT INTO receive_voucher_bank_rows (
                    id, document_root_id, row_order, bank_code, bank_account_no,
                    check_no, bill_no, currency_amt_raw, currency_amt, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    root_id,
                    row_order,
                    bank_row.get("bank_code"),
                    bank_row.get("bank_account_no"),
                    bank_row.get("check_no"),
                    bank_row.get("bill_no"),
                    bank_row.get("currency_amt_raw"),
                    bank_row.get("currency_amt"),
                    now,
                ),
            )

        conn.commit()

    return ImportResult(
        total_pages=len(staged),
        voucher_count=len(grouped),
        voucher_numbers=voucher_numbers,
        warnings=warnings,
    )
