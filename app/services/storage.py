from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from app.config import get_settings


def safe_name(value: str) -> str:
    value = value.strip().upper()
    value = re.sub(r"[^A-Z0-9._-]+", "_", value)
    return value or str(uuid4())


def write_receive_voucher_page(voucher_number: str, page_order: int, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "receive_vouchers" / "pages" / safe_name(voucher_number)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page_order:03d}.pdf"
    path.write_bytes(data)
    return str(path)


def write_receive_voucher_assembled(voucher_number: str, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "receive_vouchers" / "assembled"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name(voucher_number)}.pdf"
    path.write_bytes(data)
    return str(path)


def write_invoice_page(invoice_number: str, page_order: int, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "invoices" / "pages" / safe_name(invoice_number)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page_order:03d}.pdf"
    path.write_bytes(data)
    return str(path)


def write_invoice_assembled(invoice_number: str, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "invoices" / "assembled"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name(invoice_number)}.pdf"
    path.write_bytes(data)
    return str(path)


def write_credit_memo_page(credit_memo_number: str, page_order: int, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "credit_memos" / "pages" / safe_name(credit_memo_number)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page_order:03d}.pdf"
    path.write_bytes(data)
    return str(path)


def write_credit_memo_assembled(credit_memo_number: str, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / "credit_memos" / "assembled"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name(credit_memo_number)}.pdf"
    path.write_bytes(data)
    return str(path)


def _write_statement_page(kind: str, key: str, page_order: int, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / kind / "pages" / safe_name(key)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page_order:03d}.pdf"
    path.write_bytes(data)
    return str(path)


def _write_statement_assembled(kind: str, key: str, data: bytes) -> str:
    settings = get_settings()
    folder = settings.pdf_storage_dir / kind / "assembled"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_name(key)}.pdf"
    path.write_bytes(data)
    return str(path)


def write_kbank_statement_page(statement_reference: str, page_order: int, data: bytes) -> str:
    return _write_statement_page("kbank_statements", statement_reference, page_order, data)


def write_kbank_statement_assembled(statement_reference: str, data: bytes) -> str:
    return _write_statement_assembled("kbank_statements", statement_reference, data)


def write_uob_ca_statement_page(statement_key: str, page_order: int, data: bytes) -> str:
    return _write_statement_page("uob_ca_statements", statement_key, page_order, data)


def write_uob_ca_statement_assembled(statement_key: str, data: bytes) -> str:
    return _write_statement_assembled("uob_ca_statements", statement_key, data)


def read_pdf_from_storage(blob: bytes | None, path: str | None) -> bytes:
    if blob is not None:
        return blob
    if path:
        return Path(path).read_bytes()
    raise ValueError("PDF data is missing")
