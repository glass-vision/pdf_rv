from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


STATEMENT_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*Account\s*Statement", re.IGNORECASE)
PERIOD_RE = re.compile(
    r"Movement\s*Details\s*-\s*From:\s*(\d{1,2}\s*[A-Za-z]{3}\s*\d{4})"
    r"\s*(?:To|-\s*To)\s*:?\s*(\d{1,2}\s*[A-Za-z]{3}\s*\d{4})",
    re.IGNORECASE,
)
COMPANY_ID_RE = re.compile(r"Company\s*ID\s*:\s*(\S+)", re.IGNORECASE)
ACCOUNT_NO_RE = re.compile(r"Account\s*Number\s*:\s*([0-9][0-9\s-]{6,}[0-9])", re.IGNORECASE)
ACCOUNT_NAME_RE = re.compile(
    r"Account\s*Name\s*:\s*(.+?)(?=\s*(?:Ledger\s*Balance|Account\s*Type|"
    r"Account\s*Currency|Account\s*Branch|Movement\s*Details|$))",
    re.IGNORECASE,
)
ACCOUNT_TYPE_RE = re.compile(
    r"Account\s*Type\s*:\s*(.+?)(?=\s*(?:Available\s*Balance|Account\s*Currency|"
    r"Account\s*Branch|Movement\s*Details|$))",
    re.IGNORECASE,
)
CURRENCY_RE = re.compile(r"Account\s*Currency\s*:\s*(\S+)", re.IGNORECASE)
BRANCH_RE = re.compile(
    r"Account\s*Branch\s*:\s*(.+?)(?=\s*(?:Overdraft\s*Facility|"
    r"Account\s*Nature|Movement\s*Details|$))",
    re.IGNORECASE,
)
TRANSACTION_ID_RE = re.compile(r"\b\d{10}\b")
# Real UOB transaction refs in the statement data use a repeated two-digit
# bank/channel prefix around "B", followed by an alphanumeric identifier,
# e.g. 04B04B1A7570557. Keeping this shape explicit prevents compacted header
# labels and account/branch names from being treated as transaction refs.
TRANSACTION_REF_RE = re.compile(r"\b\d{2}B\d{2}B[A-Z0-9]{6,15}\b", re.IGNORECASE)
CUSTOMER_REF_RE = re.compile(r"\b[A-Z0-9]{8,30}013807\b", re.IGNORECASE)
TRANSACTION_TYPE_RE = re.compile(r"\b(?:MISC|TRF|CHQ|CASH|FEE|INT)(?:\s+[A-Z]{2,12}){0,3}\b", re.IGNORECASE)
MONEY_VALUE_RE = r"-?\d{1,3}(?:,\d{3})*\.\d{2}|-?\d+\.\d{2}"
UOB_TRANSACTION_START_RE = re.compile(
    rf"^(\d{{2}}/\d{{2}}/\d{{4}})\s+"
    rf"(\d{{2}}/\d{{2}}/\d{{4}})\s+"
    rf"(\d{{2}}/\d{{2}}/\d{{4}})\s+"
    rf"([A-Z0-9]+)\s+"
    rf"({MONEY_VALUE_RE})\s+"
    rf"({MONEY_VALUE_RE})\s+"
    rf"({MONEY_VALUE_RE})$",
    re.IGNORECASE,
)
TIME_AND_ID_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2}[AP]M)\s+(\d{10})\b", re.IGNORECASE)


@dataclass(slots=True)
class UobCaStatementExtraction:
    statement_key: str
    statement_date: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    company_id: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    account_type: str | None = None
    account_currency: str | None = None
    account_branch: str | None = None
    refs: dict[str, set[str]] = field(default_factory=dict)


@dataclass(slots=True)
class UobCaStatementHeader:
    statement_date: str | None = None
    company_id: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    account_type: str | None = None
    account_currency: str | None = None
    account_branch: str | None = None
    period_from: str | None = None
    period_to: str | None = None


@dataclass(slots=True)
class UobCaStatementTransaction:
    row_order: int
    transaction_date: str
    value_date: str
    posting_date: str
    transaction_time: str | None
    transaction_type: str
    description: str | None
    deposit_raw: str
    deposit: str
    withdrawal_raw: str
    withdrawal: str
    amount_raw: str
    amount: str
    balance_raw: str
    balance: str
    transaction_id: str | None
    transaction_ref: str | None
    customer_ref: str | None
    customer_code: str | None
    source_line: str


def normalize_ref(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def parse_english_date(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip()).title()
    for fmt in ("%d %b %Y", "%d%b%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Invalid English date: {value}")


def parse_numeric_date(value: str) -> str:
    return datetime.strptime(value, "%d/%m/%Y").date().isoformat()


def normalize_money(value: str) -> str:
    return f"{Decimal(value.replace(',', '').strip()):.2f}"


def derive_statement_key(account_number: str, period_from: str, period_to: str) -> str:
    return f"UOB-CA-{normalize_ref(account_number)}-{period_from.replace('-', '')}-{period_to.replace('-', '')}"


def _signed_transaction_amount(
    deposit_raw: str,
    deposit: str,
    withdrawal_raw: str,
    withdrawal: str,
) -> tuple[str, str]:
    deposit_amount = Decimal(deposit)
    withdrawal_amount = Decimal(withdrawal)
    if deposit_amount != 0:
        return deposit_raw, deposit
    if withdrawal_amount != 0:
        return withdrawal_raw, f"{-withdrawal_amount:.2f}"
    return deposit_raw, "0.00"


def _build_transaction(row_order: int, lines: list[str]) -> UobCaStatementTransaction | None:
    if not lines:
        return None
    start = UOB_TRANSACTION_START_RE.match(lines[0])
    if not start:
        return None

    deposit_raw = start.group(5)
    withdrawal_raw = start.group(6)
    deposit = normalize_money(deposit_raw)
    withdrawal = normalize_money(withdrawal_raw)
    amount_raw, amount = _signed_transaction_amount(deposit_raw, deposit, withdrawal_raw, withdrawal)
    source_line = "\n".join(lines)
    transaction_time = None
    transaction_id = None
    time_and_id = TIME_AND_ID_RE.search(source_line)
    if time_and_id:
        transaction_time = time_and_id.group(1).upper()
        transaction_id = time_and_id.group(2)

    transaction_ref_match = TRANSACTION_REF_RE.search(source_line.upper())
    transaction_ref = transaction_ref_match.group(0).upper() if transaction_ref_match else None
    customer_ref_match = CUSTOMER_REF_RE.search(source_line.upper())
    customer_ref = customer_ref_match.group(0).upper() if customer_ref_match else None
    customer_code = customer_ref[:-6] if customer_ref and len(customer_ref) > 6 else None

    return UobCaStatementTransaction(
        row_order=row_order,
        transaction_date=parse_numeric_date(start.group(1)),
        value_date=parse_numeric_date(start.group(2)),
        posting_date=parse_numeric_date(start.group(3)),
        transaction_time=transaction_time,
        transaction_type=start.group(4).upper(),
        description=None,
        deposit_raw=deposit_raw,
        deposit=deposit,
        withdrawal_raw=withdrawal_raw,
        withdrawal=withdrawal,
        amount_raw=amount_raw,
        amount=amount,
        balance_raw=start.group(7),
        balance=normalize_money(start.group(7)),
        transaction_id=transaction_id,
        transaction_ref=transaction_ref,
        customer_ref=customer_ref,
        customer_code=customer_code,
        source_line=source_line,
    )


def extract_uob_ca_statement_transactions(text: str) -> list[UobCaStatementTransaction]:
    transactions: list[UobCaStatementTransaction] = []
    current: list[str] = []

    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if UOB_TRANSACTION_START_RE.match(line):
            built = _build_transaction(len(transactions) + 1, current)
            if built:
                transactions.append(built)
            current = [line]
            continue
        if current:
            if re.fullmatch(r"\d+/\d+", line):
                continue
            if STATEMENT_DATE_RE.search(line):
                continue
            current.append(line)

    built = _build_transaction(len(transactions) + 1, current)
    if built:
        transactions.append(built)
    return transactions


def _match(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    if pattern is ACCOUNT_NO_RE:
        return normalize_ref(m.group(1))
    return m.group(1).strip()


def _extract_account_number(text: str) -> str | None:
    match = ACCOUNT_NO_RE.search(text)
    if not match:
        return None
    return normalize_ref(match.group(1))


def _extract_period(text: str) -> tuple[str, str] | None:
    period = PERIOD_RE.search(text)
    if not period:
        return None
    return parse_english_date(period.group(1)), parse_english_date(period.group(2))


def _extract_header(text: str) -> UobCaStatementHeader:
    period = _extract_period(text)
    statement_date_match = STATEMENT_DATE_RE.search(text)
    return UobCaStatementHeader(
        statement_date=parse_numeric_date(statement_date_match.group(1)) if statement_date_match else None,
        company_id=_match(COMPANY_ID_RE, text),
        account_number=_extract_account_number(text),
        account_name=_match(ACCOUNT_NAME_RE, text),
        account_type=_match(ACCOUNT_TYPE_RE, text),
        account_currency=_match(CURRENCY_RE, text),
        account_branch=_match(BRANCH_RE, text),
        period_from=period[0] if period else None,
        period_to=period[1] if period else None,
    )


def _extract_transaction_refs(
    text: str,
    account_number: str,
    company_id: str | None,
    account_name: str | None = None,
    statement_date: str | None = None,
    period_to: str | None = None,
) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {
        "account_number": {account_number},
        "bank_code": {"UOB"},
    }
    if company_id:
        refs["company_id"] = {company_id}
    if account_name:
        refs["account_name"] = {account_name}
    if statement_date:
        refs["statement_date"] = {statement_date}
    if period_to:
        refs["period_to"] = {period_to}
    excluded = {normalize_ref(account_number), normalize_ref(company_id or "")}
    transaction_ids = {
        v for v in TRANSACTION_ID_RE.findall(text) if normalize_ref(v) not in excluded
    }
    customer_refs = {v.upper() for v in CUSTOMER_REF_RE.findall(text)}
    transaction_refs = {
        v for v in TRANSACTION_REF_RE.findall(text.upper())
        if v not in customer_refs and v not in excluded
    }
    transaction_types = {v.upper() for v in TRANSACTION_TYPE_RE.findall(text)}
    customer_codes = {v[:-6] for v in customer_refs if len(v) > 6}
    if transaction_ids:
        refs["transaction_id"] = transaction_ids
    if transaction_refs:
        refs["transaction_ref"] = transaction_refs
    if customer_refs:
        refs["customer_ref"] = customer_refs
    if customer_codes:
        refs["customer_code"] = customer_codes
    if transaction_types:
        refs["transaction_type"] = transaction_types
    return refs


def extract_uob_ca_statement_header(text: str) -> UobCaStatementHeader:
    """Extract any available header fields from a page."""
    return _extract_header(text)


def extract_uob_ca_statement_refs(
    text: str,
    account_number: str,
    company_id: str | None,
    account_name: str | None = None,
    statement_date: str | None = None,
    period_to: str | None = None,
) -> dict[str, set[str]]:
    """Extract refs using the known account context."""
    return _extract_transaction_refs(text, account_number, company_id, account_name, statement_date, period_to)


def extract_uob_ca_statement(text: str) -> UobCaStatementExtraction | None:
    """Extract from a page that contains the full account header."""
    account_number = _extract_account_number(text)
    period = _extract_period(text)
    if not account_number or not period:
        return None

    period_from, period_to = period
    statement_key = derive_statement_key(account_number, period_from, period_to)
    statement_date_match = STATEMENT_DATE_RE.search(text)
    statement_date = parse_numeric_date(statement_date_match.group(1)) if statement_date_match else None
    company_id = _match(COMPANY_ID_RE, text)
    account_name = _match(ACCOUNT_NAME_RE, text)

    return UobCaStatementExtraction(
        statement_key=statement_key,
        statement_date=statement_date,
        period_from=period_from,
        period_to=period_to,
        company_id=company_id,
        account_number=account_number,
        account_name=account_name,
        account_type=_match(ACCOUNT_TYPE_RE, text),
        account_currency=_match(CURRENCY_RE, text),
        account_branch=_match(BRANCH_RE, text),
        refs=_extract_transaction_refs(text, account_number, company_id, account_name, statement_date, period_to),
    )


def extract_uob_ca_statement_context(text: str) -> tuple[str, str] | None:
    """Extract the account number and period when a page only has partial header data."""
    account_number = _extract_account_number(text)
    period = _extract_period(text)
    if not account_number or not period:
        return None
    return account_number, derive_statement_key(account_number, period[0], period[1])


def extract_uob_ca_statement_period(text: str) -> tuple[str, str] | None:
    """Extract the period from a page when the account header is not present."""
    return _extract_period(text)


def extract_uob_ca_statement_header_context(text: str) -> UobCaStatementHeader:
    """Extract partial UOB CA header fields from any page."""
    return _extract_header(text)


def make_stateful_extractor() -> Callable[[str], UobCaStatementExtraction | None]:
    """Return a per-import extractor that carries header context across pages.

    UOB CA PDFs can omit the full header on continuation pages. This closure
    caches the first full extraction and re-uses it for subsequent pages that
    share the same period.
    """
    _ctx: list[UobCaStatementExtraction | None] = [None]

    def _extract(text: str) -> UobCaStatementExtraction | None:
        result = extract_uob_ca_statement(text)
        if result:
            _ctx[0] = result
            return result
        ctx = _ctx[0]
        period = _extract_period(text)
        if not period or not ctx:
            return None
        period_from, period_to = period
        if period_from != ctx.period_from or period_to != ctx.period_to:
            return None
        return UobCaStatementExtraction(
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
            refs=_extract_transaction_refs(text, ctx.account_number or "", ctx.company_id),
        )

    return _extract
