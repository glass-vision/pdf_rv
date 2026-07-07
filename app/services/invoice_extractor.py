from __future__ import annotations

import re
from dataclasses import dataclass, field


# Document No. / Sales Order No. examples: ST2505-0001, T42507-0002,
# F72506-1201, NT2505-0001. Same shape as Receive Voucher's DOC_NO_RE.
DOC_NO_RE = re.compile(r"\b[A-Z]{1,3}\d{4,8}-\d{3,5}(?:\(\d+\))?\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
CUSTOMER_LABEL_RE = re.compile(r"Customer\s*[:\s]+(\S+)", re.IGNORECASE)
EXT_DOC_NO_LABEL_RE = re.compile(r"Ext\.?\s*Doc\.?\s*No\.?\s*:?\s*(.*)", re.IGNORECASE)


@dataclass(slots=True)
class InvoiceExtraction:
    invoice_number: str
    invoice_date: str | None = None
    name: str | None = None
    customer_code: str | None = None
    refs: dict[str, set[str]] = field(default_factory=dict)


def normalize_ref(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def normalize_invoice_no(value: str) -> str:
    value = normalize_ref(value)
    return re.sub(r"\(\d+\)$", "", value)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]


def _block_after(text: str, marker: str) -> str:
    idx = text.lower().find(marker.lower())
    if idx < 0:
        return text
    return text[idx:]


def extract_invoice_number(text: str) -> str | None:
    """The root key: the first Document No.-shaped match on the page."""
    matches = DOC_NO_RE.findall(text)
    return normalize_invoice_no(matches[0]) if matches else None


def extract_invoice_date(text: str) -> str | None:
    after = _block_after(text, "Document Date")
    match = DATE_RE.search(after)
    return match.group(1) if match else None


def extract_customer_code_and_name(text: str) -> tuple[str | None, str | None]:
    """customer_code follows the "Customer" label; name is the next line."""
    lines = _lines(text)
    for index, line in enumerate(lines):
        match = CUSTOMER_LABEL_RE.search(line)
        if not match:
            continue
        code = normalize_ref(match.group(1)) or None
        name = lines[index + 1] if index + 1 < len(lines) else None
        return code, name
    return None, None


def extract_ext_doc_no(text: str) -> str | None:
    for line in _lines(text):
        match = EXT_DOC_NO_LABEL_RE.search(line)
        if match:
            value = match.group(1).strip()
            if value:
                return normalize_ref(value)
    return None


def extract_refs(text: str, invoice_number: str) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {
        "sales_order_no": set(),
        "customer_code": set(),
        "ext_doc_no": set(),
    }

    invoice_norm = normalize_invoice_no(invoice_number)
    for raw in DOC_NO_RE.findall(text):
        normalized = normalize_invoice_no(raw)
        if normalized != invoice_norm:
            refs["sales_order_no"].add(normalized)

    customer_code, _ = extract_customer_code_and_name(text)
    if customer_code:
        refs["customer_code"].add(customer_code)

    ext_doc_no = extract_ext_doc_no(text)
    if ext_doc_no:
        refs["ext_doc_no"].add(ext_doc_no)

    return {k: v for k, v in refs.items() if v}


def extract_invoice(text: str) -> InvoiceExtraction | None:
    invoice_number = extract_invoice_number(text)
    if not invoice_number:
        return None

    customer_code, name = extract_customer_code_and_name(text)
    return InvoiceExtraction(
        invoice_number=invoice_number,
        invoice_date=extract_invoice_date(text),
        name=name,
        customer_code=customer_code,
        refs=extract_refs(text, invoice_number),
    )
