from __future__ import annotations

import re
from dataclasses import dataclass, field


DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
# A customer code inside "(...)" is either:
# - 7-12 digits, e.g. (1020040220)
# - an alphanumeric/hyphen code, e.g. (WALKIN), (WALKIN-O), (WALK-IN),
#   (OTHER-2001100006)
# Some PDFs misplace a Thai diacritic right after "(" before the code,
# e.g. "แคร(์2320210026)". Tolerate up to two such stray characters.
CUSTOMER_CODE_GROUP_RE = re.compile(r"^[^\dA-Za-z]{0,2}([A-Za-z0-9][A-Za-z0-9\-]*)$")
BANK_RE = re.compile(r"\b(KBANK|UOB|SCB|BBL|KTB|BAY|TTB|GSB|CIMB|TISCO)\b", re.IGNORECASE)
CHECK_LABEL_RE = re.compile(r"Check\s+No\s*:?\s*(.*)", re.IGNORECASE)
# Real check numbers are numeric, optionally with a "#<period>" suffix, e.g.
# "88015386#2508" (matches the plain-numeric check numbers KBank statements
# use once the suffix is stripped). Values like "A1250801-0046" that show up
# in the payment/transaction table below a blank "Check No :" are a
# different, unrelated reference (letter + date + dash + sequence) and must
# never be accepted here even though they share a superficially similar
# alphanumeric-with-dash look.
CHECK_NO_VALUE_RE = re.compile(r"^\d+(?:-\d+)?(?:#\d+)?$")
ACCOUNT_NO_RE = re.compile(r"\b\d{3}-\d-\d{5}-\d\b")
# Voucher numbers seen in Receive Voucher samples include QR2508-00001,
# RV..., and R... forms. Keep this deliberately broader than the invoice regex.
VOUCHER_CANDIDATE_RE = re.compile(r"\b(?:QR|RV|R)\d{4,8}-\d{3,5}\b", re.IGNORECASE)
# Invoice examples: SL2505-0023, FB2505-0003, S52205-0040, T52506-0030, QR2507-00109.
DOC_NO_RE = re.compile(r"\b[A-Z]{1,3}\d{4,8}-\d{3,5}(?:\(\d+\))?\b", re.IGNORECASE)
BILL_NO_RE = re.compile(r"\b[A-Z]\d{6,8}-\d{3,5}\b", re.IGNORECASE)
BILL_NO_WRAP_PREFIX_RE = re.compile(r"^[A-Z]\d{6,8}-$", re.IGNORECASE)
BILL_NO_WRAP_CONTINUATION_RE = re.compile(r"^\d{3,5}$")
TRANSACTION_ROW_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
MONEY_RE = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")
TRAILING_CURRENCY_AMOUNT_RE = re.compile(r"\b[A-Z]{3}\s+(\d{1,3}(?:,\d{3})*\.\d{2})\b")
ACCOUNT_ROW_PREFIX_RE = re.compile(r"^\d{6,8}\b")
JOURNAL_NAME_LABEL_RE = re.compile(r"Journal\s+Name\s*:?\s*(.*)", re.IGNORECASE)
JOURNAL_NAME_VALUE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+\b", re.IGNORECASE)
LABEL_ONLY_RE = re.compile(r"^[A-Z][A-Z\s.]+:\s*$", re.IGNORECASE)


@dataclass(slots=True)
class ReceiveVoucherExtraction:
    voucher_number: str
    voucher_date: str | None = None
    name: str | None = None
    customer_code: str | None = None
    refs: dict[str, set[str]] = field(default_factory=dict)
    bank_rows: list[dict[str, str]] = field(default_factory=list)


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


def _block_before(text: str, marker: str) -> str:
    idx = text.lower().find(marker.lower())
    if idx < 0:
        return text
    return text[:idx]


def _block_between(text: str, start_marker: str, end_marker: str) -> str:
    return _block_before(_block_after(text, start_marker), end_marker)


def _header_block(text: str) -> str:
    """The document header area (Name, Journal Name, Voucher Number, etc.),
    excluding the transaction/payment tables below it. Customer codes in
    "(...)" only appear in this area; the tables below can contain
    unrelated "(...)" values such as "(THB)" or "(Other)"."""
    block = _block_before(text, "Business Gr")
    return _block_before(block, "Payment Details")


def _looks_like_label(value: str) -> bool:
    stripped = value.strip()
    return not stripped or bool(LABEL_ONLY_RE.match(stripped)) or stripped.lower().endswith(":")


def _is_valid_check_no(value: str) -> bool:
    normalized = normalize_ref(value.rstrip(":"))
    return bool(CHECK_NO_VALUE_RE.fullmatch(normalized))


PAREN_GROUP_RE = re.compile(r"\(([^()]*)\)")


def _customer_code_from_content(content: str) -> str | None:
    match = CUSTOMER_CODE_GROUP_RE.match(content)
    if not match:
        return None
    core = match.group(1)
    if core.isdigit() and not (7 <= len(core) <= 12):
        return None
    return core.upper()


def _last_customer_code_match(text: str) -> tuple[str, str] | None:
    """Return (full "(...)" text, normalized code) for the last customer
    code found in text, or None."""
    best = None
    for match in PAREN_GROUP_RE.finditer(text):
        code = _customer_code_from_content(match.group(1))
        if code:
            best = (match.group(0), code)
    return best


def _strip_journal_name(line: str) -> str:
    return re.sub(r"(?i)\s*journal\s+name\s*:.*$", "", line)


def _join_wrapped_parens(line: str, next_line: str | None) -> str:
    """Join with the next line when "(...)" is split across a line wrap,
    e.g. "...(OTHER-" on one line and "2001100006)" on the next."""
    if next_line is not None and line.count("(") > line.count(")"):
        return line + next_line
    return line


def extract_voucher_number(text: str) -> str | None:
    """Extract the root key for Receive Voucher.

    Prefer candidates after the "Voucher Number" label. Fallback to the first
    QR/RV/R-style candidate in the page text.
    """
    after = _block_after(text, "Voucher Number")
    candidates = VOUCHER_CANDIDATE_RE.findall(after)
    if candidates:
        return normalize_ref(candidates[0])
    all_candidates = VOUCHER_CANDIDATE_RE.findall(text)
    return normalize_ref(all_candidates[0]) if all_candidates else None


def extract_voucher_date(text: str) -> str | None:
    after = _block_after(text, "Voucher Date")
    match = DATE_RE.search(after)
    return match.group(1) if match else None


def extract_name(text: str) -> tuple[str | None, str | None]:
    """Best-effort extraction for Name and customer code.

    The sample PDF places the Name value after the label block and before
    payment method values. This extractor is conservative; failed name extraction
    must not fail the import.

    The search is limited to the document header area so that "(...)"
    values in the transaction/payment tables (e.g. "(THB)", "(Other)")
    are not mistaken for the customer code.
    """
    lines = _lines(_header_block(text))
    candidate_indexes = []
    skip_next = False
    for index, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        stripped = _strip_journal_name(line)
        will_join = next_line is not None and stripped.count("(") > stripped.count(")")
        window = _join_wrapped_parens(stripped, next_line)
        if "(" in window and ")" in window and _last_customer_code_match(window):
            if not any(skip in line.lower() for skip in ["page :", "total :", "check"]):
                candidate_indexes.append(index)
                if will_join:
                    # The next line was consumed to close the "(...)" on
                    # this line; don't treat it as its own candidate too
                    # (e.g. ")(2420220022)" left over after the join).
                    skip_next = True
    if not candidate_indexes:
        return None, None

    index = candidate_indexes[-1]
    next_line = lines[index + 1] if index + 1 < len(lines) else None
    line = lines[index]
    is_name_line = bool(re.match(r"(?i)^\s*name\s*:", line))
    value = re.sub(r"(?i)^\s*name\s*:\s*", "", line).strip()
    value = _strip_journal_name(value).strip()
    value = _join_wrapped_parens(value, next_line)

    code_match = _last_customer_code_match(value)
    code = code_match[1] if code_match else None
    name = value.replace(code_match[0], "", 1).strip() if code_match else value

    if not is_name_line and index > 0:
        # The Name wraps mid-word across two lines, e.g.
        # "Name : ... หมอประภา\nพร(2120200020)" -> "...หมอประภาพร". Prepend
        # the preceding "Name :" line's text (PDF wraps with no space).
        prev_line = lines[index - 1]
        if re.match(r"(?i)^\s*name\s*:", prev_line):
            prev_value = re.sub(r"(?i)^\s*name\s*:\s*", "", prev_line).strip()
            prev_value = _strip_journal_name(prev_value).strip()
            name = (prev_value + name).strip()

    return name or None, code


def extract_check_no(text: str) -> str | None:
    lines = _lines(text)
    for index, line in enumerate(lines):
        match = CHECK_LABEL_RE.search(line)
        if not match:
            continue

        # The label and the next field can share one line, e.g.
        # "Check No : 88015386#2508 Invoice No. : SL2505-0023" - the check
        # number is only the first token, not the whole line remainder.
        inline_value = match.group(1).strip()
        first_token = inline_value.split(maxsplit=1)[0] if inline_value else ""
        if first_token and _is_valid_check_no(first_token):
            return normalize_ref(first_token)

        for candidate in lines[index + 1 : index + 5]:
            if _looks_like_label(candidate):
                break
            if _is_valid_check_no(candidate):
                return normalize_ref(candidate)
    return None


def extract_journal_name(text: str, voucher_number: str) -> str | None:
    for line in _lines(text):
        match = JOURNAL_NAME_LABEL_RE.search(line)
        if not match:
            continue
        value = match.group(1).strip()
        if value and not _looks_like_label(value):
            return value

    voucher_norm = normalize_ref(voucher_number)
    after = _block_after(text, "Journal Name")
    for candidate in JOURNAL_NAME_VALUE_RE.findall(after):
        normalized = normalize_ref(candidate)
        if normalized == voucher_norm:
            continue
        if VOUCHER_CANDIDATE_RE.fullmatch(candidate):
            continue
        if DOC_NO_RE.fullmatch(candidate) or BILL_NO_RE.fullmatch(candidate):
            continue
        return candidate.strip()
    return None


def extract_bill_nos(text: str) -> set[str]:
    """Extract complete and PDF-line-wrapped Bill No. values."""
    values = {normalize_ref(raw) for raw in BILL_NO_RE.findall(text)}
    lines = _lines(text)
    for index, line in enumerate(lines):
        prefix = line.strip()
        if not BILL_NO_WRAP_PREFIX_RE.fullmatch(prefix):
            continue

        continuation_index = index + 1
        if continuation_index >= len(lines):
            continue
        continuation = lines[continuation_index].strip()

        if not BILL_NO_WRAP_CONTINUATION_RE.fullmatch(continuation):
            # The observed PDF layout places one transaction row between the
            # two Bill No. fragments. Do not skip arbitrary text or labels.
            if not TRANSACTION_ROW_DATE_RE.search(continuation):
                continue
            continuation_index += 1
            if continuation_index >= len(lines):
                continue
            continuation = lines[continuation_index].strip()

        if BILL_NO_WRAP_CONTINUATION_RE.fullmatch(continuation):
            values.add(normalize_ref(prefix + continuation))
    return values


def normalize_currency_amount(value: str) -> str:
    return value.replace(",", "").strip()


def _extract_currency_amount_from_row(row_text: str) -> tuple[str, str] | None:
    trailing = TRAILING_CURRENCY_AMOUNT_RE.search(row_text)
    if trailing:
        raw = trailing.group(1)
        return raw, normalize_currency_amount(raw)
    amounts = MONEY_RE.findall(row_text)
    if not amounts:
        return None
    raw = amounts[-1]
    return raw, normalize_currency_amount(raw)


def _extract_business_row_amount(row_text: str) -> tuple[str, str] | None:
    amounts = MONEY_RE.findall(row_text)
    if not amounts:
        return None
    raw = amounts[0]
    return raw, normalize_currency_amount(raw)


def _extract_payment_detail_bill_nos(payment_lines: list[str]) -> list[str]:
    bill_nos: list[str] = []
    index = 0
    while index < len(payment_lines):
        line = payment_lines[index].strip()
        direct_bill_nos = [normalize_ref(raw) for raw in BILL_NO_RE.findall(line)]
        if direct_bill_nos:
            bill_nos.extend(direct_bill_nos)
            index += 1
            continue

        if BILL_NO_WRAP_PREFIX_RE.fullmatch(line):
            continuation_index = index + 1
            if continuation_index >= len(payment_lines):
                break
            continuation = payment_lines[continuation_index].strip()
            if not BILL_NO_WRAP_CONTINUATION_RE.fullmatch(continuation):
                continuation_index += 1
                if continuation_index >= len(payment_lines):
                    break
                continuation = payment_lines[continuation_index].strip()
            if BILL_NO_WRAP_CONTINUATION_RE.fullmatch(continuation):
                bill_nos.append(normalize_ref(line + continuation))
                index = continuation_index + 1
                continue
        index += 1
    return bill_nos


def _extract_payment_detail_single_amount(payment_lines: list[str]) -> tuple[str, str] | None:
    candidate_amounts: list[str] = []
    for line in payment_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(("payment details", "amount", "posting date", "currency", "total :")):
            continue
        if not TRANSACTION_ROW_DATE_RE.search(stripped):
            continue
        if "THB" not in stripped.upper():
            continue
        amounts = MONEY_RE.findall(stripped)
        if not amounts:
            continue
        raw = amounts[-1]
        candidate_amounts.append(raw)
    if len(candidate_amounts) != 1:
        return None
    raw = candidate_amounts[0]
    return raw, normalize_currency_amount(raw)


def extract_bank_rows(text: str, voucher_number: str) -> list[dict[str, str]]:
    bank_codes = sorted({normalize_ref(raw) for raw in BANK_RE.findall(text)})
    bank_account_nos = sorted({normalize_ref(raw) for raw in ACCOUNT_NO_RE.findall(text)})
    check_no = extract_check_no(text)
    if not bank_codes and not bank_account_nos and not check_no:
        return []

    payment_lines = _lines(_block_after(text, "Payment Details"))
    bill_values = sorted(set(_extract_payment_detail_bill_nos(payment_lines)))
    shared_bill_no = bill_values[0] if len(bill_values) == 1 else None
    payment_detail_amount = _extract_payment_detail_single_amount(payment_lines)
    page_lines = _lines(_block_before(text, "Payment Details"))
    bank_rows: list[dict[str, str]] = []

    def build_bank_row(row_text: str) -> dict[str, str] | None:
        amount = _extract_business_row_amount(row_text)
        if not amount and payment_detail_amount:
            amount = payment_detail_amount
        if not amount:
            return None
        raw_amount, normalized_amount = amount
        row: dict[str, str] = {
            "currency_amt_raw": raw_amount,
            "currency_amt": normalized_amount,
        }
        if shared_bill_no:
            row["bill_no"] = shared_bill_no
        if bank_codes:
            for code in bank_codes:
                if code in normalize_ref(row_text):
                    row["bank_code"] = code
                    break
            else:
                row["bank_code"] = bank_codes[0]
        if bank_account_nos:
            for account_no in bank_account_nos:
                if account_no in normalize_ref(row_text):
                    row["bank_account_no"] = account_no
                    break
            else:
                row["bank_account_no"] = bank_account_nos[0]
        if check_no:
            row["check_no"] = check_no
        return row

    index = 0
    while index < len(page_lines):
        line = page_lines[index].strip()
        joined = line
        next_line = page_lines[index + 1].strip() if index + 1 < len(page_lines) else None
        if next_line and ACCOUNT_NO_RE.search(next_line) and not ACCOUNT_NO_RE.search(line):
            joined = f"{line} {next_line}"
            index += 1
        elif (
            not ACCOUNT_NO_RE.search(line)
            and index + 2 < len(page_lines)
            and not ACCOUNT_ROW_PREFIX_RE.match(page_lines[index + 1].strip())
        ):
            next_line = page_lines[index + 1].strip()
            third_line = page_lines[index + 2].strip()
            if ACCOUNT_NO_RE.search(third_line):
                joined = f"{line} {next_line} {third_line}"
                index += 2
        normalized_joined = normalize_ref(joined)
        looks_like_business_row = bool(ACCOUNT_ROW_PREFIX_RE.match(joined))
        if looks_like_business_row and (ACCOUNT_NO_RE.search(joined) or any(code in normalized_joined for code in bank_codes)):
            row = build_bank_row(joined)
            if row:
                bank_rows.append(row)
        index += 1

    deduped: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for row in bank_rows:
        signature = tuple(sorted(row.items()))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)
    return deduped


def extract_refs(text: str, voucher_number: str) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {
        "invoice_no": set(),
        "bank_code": set(),
        "bank_account_no": set(),
        "check_no": set(),
        "bill_no": set(),
        "customer_code": set(),
        "journal_name": set(),
    }

    voucher_norm = normalize_ref(voucher_number)

    bill_values = extract_bill_nos(text)

    for raw in DOC_NO_RE.findall(text):
        normalized = normalize_invoice_no(raw)
        if normalized != voucher_norm and normalized not in bill_values:
            refs["invoice_no"].add(normalized)

    for value in bill_values:
        if value != voucher_norm:
            refs["bill_no"].add(value)

    for raw in BANK_RE.findall(text):
        refs["bank_code"].add(normalize_ref(raw))

    for raw in ACCOUNT_NO_RE.findall(text):
        refs["bank_account_no"].add(normalize_ref(raw))

    check_no = extract_check_no(text)
    if check_no:
        refs["check_no"].add(check_no)

    lines = _lines(_header_block(text))
    for index, line in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        window = _join_wrapped_parens(_strip_journal_name(line), next_line)
        # Only the last "(...)" group per line/window is a candidate customer
        # code (matching extract_name), so e.g. "(Other)(WALKIN-O)" doesn't
        # also pick up "OTHER" as a separate code.
        code_match = _last_customer_code_match(window)
        if code_match:
            refs["customer_code"].add(code_match[1])

    journal_name = extract_journal_name(text, voucher_number)
    if journal_name:
        refs["journal_name"].add(journal_name)

    return {k: v for k, v in refs.items() if v}


def extract_receive_voucher(text: str) -> ReceiveVoucherExtraction | None:
    voucher_number = extract_voucher_number(text)
    if not voucher_number:
        return None

    name, customer_code = extract_name(text)
    return ReceiveVoucherExtraction(
        voucher_number=voucher_number,
        voucher_date=extract_voucher_date(text),
        name=name,
        customer_code=customer_code,
        refs=extract_refs(text, voucher_number),
        bank_rows=extract_bank_rows(text, voucher_number),
    )
