from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


TH_REF_LABEL = "\u0e40\u0e25\u0e02\u0e17\u0e35\u0e48\u0e2d\u0e49\u0e32\u0e07\u0e2d\u0e34\u0e07"
TH_CHECK_LABEL = "\u0e40\u0e0a\u0e47\u0e04\u0e40\u0e25\u0e02\u0e17\u0e35\u0e48"
TH_REF_CODE_LABEL = "\u0e23\u0e2b\u0e31\u0e2a\u0e2d\u0e49\u0e32\u0e07\u0e2d\u0e34\u0e07"
TH_ACCOUNT_NAME_LABEL = "\u0e0a\u0e37\u0e48\u0e2d\u0e1a\u0e31\u0e0d\u0e0a\u0e35"
TH_BRANCH_PREFIX = "\u0e2a\u0e32\u0e02\u0e32"
TH_BRANCH_OWNER = "\u0e40\u0e08\u0e49\u0e32\u0e02\u0e2d\u0e07\u0e1a\u0e31\u0e0d\u0e0a\u0e35"

REFERENCE_RE = re.compile(rf"{TH_REF_LABEL}\s*[:\uff1a]?\s*(\d{{12,}})")
FALLBACK_REFERENCE_RE = re.compile(r"\bN(\d{12,})O/\d{4}\b", re.IGNORECASE)
PERIOD_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
ACCOUNT_RE = re.compile(r"\b\d{3}-\d-\d{5}-\d\b")
TRANSACTION_REF_RE = re.compile(
    rf"(?:Trade\s+Ref\s+no\.?|Ref|{TH_REF_CODE_LABEL})\s*[:\uff1a]?\s*([A-Z0-9][A-Z0-9./-]{{4,}})",
    re.IGNORECASE,
)
CHECK_RE = re.compile(rf"{TH_CHECK_LABEL}\s*[:\uff1a]?\s*([A-Z0-9][A-Z0-9./-]{{2,}})", re.IGNORECASE)
STATEMENT_TXN_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{2}\b")
MONEY_RE = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")
BANK_HINT_RE = re.compile(r"\b([A-Z]{3,5}\s+\d{4})\b")


@dataclass(slots=True)
class KBankStatementExtraction:
    statement_reference: str
    period_from: str | None = None
    period_to: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    branch_name: str | None = None
    refs: dict[str, set[str]] = field(default_factory=dict)
    check_rows: list[dict[str, str]] = field(default_factory=list)


def normalize_ref(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def parse_thai_numeric_date(value: str) -> str:
    parsed = datetime.strptime(value, "%d/%m/%Y").date()
    if parsed.year > 2400:
        parsed = parsed.replace(year=parsed.year - 543)
    return parsed.isoformat()


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "\n").splitlines() if line.strip()]


def _value_after_label(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines):
        if label not in line:
            continue
        value = line.split(label, 1)[1].lstrip(" :\uff1a")
        if value:
            return value
        if index + 1 < len(lines):
            return lines[index + 1]
    return None


def _extract_account_name(lines: list[str]) -> str | None:
    direct = _value_after_label(lines, TH_ACCOUNT_NAME_LABEL)
    if direct:
        return direct
    for index, line in enumerate(lines):
        if line == TH_ACCOUNT_NAME_LABEL and index > 0:
            return lines[index - 1]
    return None


def _extract_branch_name(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith(TH_BRANCH_PREFIX) and TH_BRANCH_OWNER not in line:
            return line
    return _value_after_label(lines, TH_BRANCH_PREFIX + TH_BRANCH_OWNER)


def _parse_statement_txn_date(value: str) -> str | None:
    try:
        parsed = datetime.strptime(value, "%d-%m-%y").date()
    except ValueError:
        return None
    return parsed.isoformat()


def _iter_transaction_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if STATEMENT_TXN_DATE_RE.match(line):
            if current:
                blocks.append(" ".join(current))
            current = [line]
            continue
        if not current:
            continue
        if (
            line.startswith(")")
            or line.startswith("FDPBK")
            or line.startswith("ออกโดย")
            or line.startswith("เวลา/")
            or line.startswith("วันที่")
        ):
            blocks.append(" ".join(current))
            current = []
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return blocks


def _event_type_for_block(block: str) -> str | None:
    if TH_CHECK_LABEL not in block:
        return None
    if "ค่าธรรมเนียม" in block:
        return "fee"
    if "เช็คคืน" in block:
        return "return"
    if "ฝากด้วยเช็ค" in block:
        return "deposit"
    return "other"


def extract_kbank_check_rows(text: str) -> list[dict[str, str]]:
    lines = _lines(text)
    rows: list[dict[str, str]] = []
    for block in _iter_transaction_blocks(lines):
        event_type = _event_type_for_block(block)
        if not event_type:
            continue
        check_match = CHECK_RE.search(block)
        if not check_match:
            continue
        amounts = MONEY_RE.findall(block)
        if len(amounts) < 2:
            continue
        txn_date_match = STATEMENT_TXN_DATE_RE.match(block)
        txn_date = _parse_statement_txn_date(txn_date_match.group(0)) if txn_date_match else None
        bank_hint_match = BANK_HINT_RE.search(block)
        row = {
            "event_type": event_type,
            "check_no": check_match.group(1).rstrip(".,"),
            "amount_raw": amounts[0],
            "amount": amounts[0].replace(",", ""),
            "balance_raw": amounts[1],
            "balance": amounts[1].replace(",", ""),
            "source_line": block,
        }
        if txn_date:
            row["txn_date"] = txn_date
        if bank_hint_match:
            row["bank_hint"] = bank_hint_match.group(1)
        rows.append(row)
    return rows


def extract_statement_reference(text: str) -> str | None:
    match = REFERENCE_RE.search(text) or FALLBACK_REFERENCE_RE.search(text)
    return match.group(1) if match else None


def extract_kbank_statement(text: str) -> KBankStatementExtraction | None:
    statement_reference = extract_statement_reference(text)
    if not statement_reference:
        return None

    lines = _lines(text)
    period = PERIOD_RE.search(text)
    account = ACCOUNT_RE.search(text)
    account_name = _extract_account_name(lines)
    branch_name = _extract_branch_name(lines)
    period_to = parse_thai_numeric_date(period.group(2)) if period else None
    refs: dict[str, set[str]] = {
        "statement_reference": {statement_reference},
        "bank_code": {"KBANK"},
    }
    if account:
        refs["account_number"] = {account.group(0)}
    if account_name:
        refs["account_name"] = {account_name}
    if branch_name:
        refs["branch_name"] = {branch_name}
    if period_to:
        refs["period_to"] = {period_to}

    transaction_refs = {match.group(1).rstrip(".,") for match in TRANSACTION_REF_RE.finditer(text)}
    checks = {match.group(1).rstrip(".,") for match in CHECK_RE.finditer(text)}
    if transaction_refs:
        refs["transaction_ref"] = transaction_refs
    if checks:
        refs["check_no"] = checks

    return KBankStatementExtraction(
        statement_reference=statement_reference,
        period_from=parse_thai_numeric_date(period.group(1)) if period else None,
        period_to=period_to,
        account_number=account.group(0) if account else None,
        account_name=account_name,
        branch_name=branch_name,
        refs=refs,
        check_rows=extract_kbank_check_rows(text),
    )
