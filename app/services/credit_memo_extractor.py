from __future__ import annotations

import re
from dataclasses import dataclass, field


# Credit Memo No. examples: C12302-0024 (Layout A), CZ2506-0001 (Layout B).
# Same shape as Invoices' DOC_NO_RE.
DOC_NO_RE = re.compile(r"\b[A-Z]{1,3}\d{4,8}-\d{3,5}(?:\(\d+\))?\b", re.IGNORECASE)
# Layout A prints the Credit Memo Date as DD/MM/YYYY; Layout B prints it as
# DD/MM/YY (2-digit year).
DATE_RE_4 = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
DATE_RE_2 = re.compile(r"\b(\d{2}/\d{2}/\d{2})\b")
# Layout B: "ชื่อผู้ซื้อ/Customer <code>" followed by the name on the next line.
CUSTOMER_LABEL_RE = re.compile(r"Customer\s*[:\s]+(\S+)", re.IGNORECASE)
# Layout A: "ชื่อลูกค้า <name>", no customer code printed.
NAME_LABEL_RE = re.compile(r"ชื่อลูกค้า\s*(.*)")


@dataclass(slots=True)
class CreditMemoExtraction:
    credit_memo_number: str
    credit_memo_date: str | None = None
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


def extract_credit_memo_number(text: str) -> str | None:
    """The root key: the first Credit Memo No.-shaped match on the page."""
    matches = DOC_NO_RE.findall(text)
    return normalize_invoice_no(matches[0]) if matches else None


def extract_credit_memo_date(text: str) -> str | None:
    """The Credit Memo's own date.

    Both layouts print it after the first "วันที่" label on the page. The
    referenced invoice's date is printed later under "วันที่อ้างอิง" (which
    also contains "วันที่"), so searching from the first occurrence finds the
    Credit Memo's own date first. Layout A uses DD/MM/YYYY; Layout B uses
    DD/MM/YY.
    """
    after = _block_after(text, "วันที่")
    matches = [
        match
        for match in (DATE_RE_4.search(after), DATE_RE_2.search(after))
        if match is not None
    ]
    if not matches:
        return None
    return min(matches, key=lambda match: match.start()).group(1)


def extract_customer_code_and_name(text: str) -> tuple[str | None, str | None]:
    """Layout B: customer_code follows the "Customer" label, name is the next
    line. Layout A: no customer code, only "ชื่อลูกค้า <name>"."""
    lines = _lines(text)
    for index, line in enumerate(lines):
        match = CUSTOMER_LABEL_RE.search(line)
        if match:
            code = normalize_ref(match.group(1)) or None
            name = lines[index + 1] if index + 1 < len(lines) else None
            return code, name

    for line in lines:
        match = NAME_LABEL_RE.search(line)
        if match:
            name = match.group(1).strip()
            if name:
                return None, name

    return None, None


def extract_refs(text: str, credit_memo_number: str) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {
        "invoice_no": set(),
        "customer_code": set(),
    }

    credit_memo_norm = normalize_ref(credit_memo_number)
    for raw in DOC_NO_RE.findall(text):
        normalized = normalize_invoice_no(raw)
        if normalized != credit_memo_norm:
            refs["invoice_no"].add(normalized)

    customer_code, _ = extract_customer_code_and_name(text)
    if customer_code:
        refs["customer_code"].add(customer_code)

    return {k: v for k, v in refs.items() if v}


def extract_credit_memo(text: str) -> CreditMemoExtraction | None:
    credit_memo_number = extract_credit_memo_number(text)
    if not credit_memo_number:
        return None

    customer_code, name = extract_customer_code_and_name(text)
    return CreditMemoExtraction(
        credit_memo_number=credit_memo_number,
        credit_memo_date=extract_credit_memo_date(text),
        name=name,
        customer_code=customer_code,
        refs=extract_refs(text, credit_memo_number),
    )
