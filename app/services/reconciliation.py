from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from sqlite3 import Connection
from typing import Any


def normalize_account(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", (value or "").strip()).upper()


def normalize_amount(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def normalize_bank_code(value: str | None) -> str | None:
    normalized = re.sub(r"[^0-9A-Za-z]+", "", (value or "").strip()).upper()
    return normalized or None


def allows_uob_reconciliation(bank_code: str | None) -> bool:
    normalized = normalize_bank_code(bank_code)
    if not normalized:
        return True
    return normalized == "UOB"


def _status_for_candidate_count(count: int) -> str:
    if count == 0:
        return "no_match"
    if count == 1:
        return "unique_match"
    return "ambiguous_match"


def _fetch_rv_bank_rows(conn: Connection, voucher_number: str | None) -> list[dict[str, Any]]:
    params: list[str] = []
    where = ["dr.doc_type = 'receive-vouchers'"]
    if voucher_number:
        where.append("dr.root_key = ?")
        params.append(voucher_number.strip().upper())
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                dr.root_key AS voucher_number,
                br.id AS bank_row_id,
                br.row_order,
                br.bank_code,
                br.bank_account_no,
                br.check_no,
                br.bill_no,
                br.currency_amt_raw,
                br.currency_amt
            FROM receive_voucher_bank_rows br
            JOIN document_roots dr ON dr.id = br.document_root_id
            WHERE {" AND ".join(where)}
            ORDER BY dr.root_key, br.row_order
            """,
            params,
        ).fetchall()
    ]


def _fetch_uob_transactions(conn: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                dr.root_key AS statement_key,
                dr.customer_code AS account_number,
                tx.page_order,
                tx.row_order,
                tx.transaction_date,
                tx.transaction_time,
                tx.transaction_type,
                tx.transaction_id,
                tx.transaction_ref,
                tx.customer_ref,
                tx.customer_code,
                tx.deposit_raw,
                tx.deposit,
                tx.withdrawal_raw,
                tx.withdrawal,
                tx.amount_raw,
                tx.amount,
                tx.balance_raw,
                tx.balance,
                tx.source_line
            FROM uob_ca_statement_transactions tx
            JOIN document_roots dr ON dr.id = tx.document_root_id
            WHERE dr.doc_type = 'uob-ca-statements'
            ORDER BY dr.root_key, tx.page_order, tx.row_order
            """
        ).fetchall()
    ]


def _candidate_payload(transaction: dict[str, Any]) -> dict[str, Any]:
    page_key = f"{transaction['statement_key']}:{transaction['page_order']}"
    return {
        "statement_key": transaction["statement_key"],
        "key": page_key,
        "page_key": page_key,
        "pdf_url": f"/api/uob-ca-statements/{transaction['statement_key']}.pdf",
        "page_pdf_url": f"/api/uob-ca-statements/pages/{page_key}.pdf",
        "page_order": transaction["page_order"],
        "row_order": transaction["row_order"],
        "transaction_date": transaction["transaction_date"],
        "transaction_time": transaction["transaction_time"],
        "transaction_type": transaction["transaction_type"],
        "transaction_id": transaction["transaction_id"],
        "transaction_ref": transaction["transaction_ref"],
        "customer_ref": transaction["customer_ref"],
        "customer_code": transaction["customer_code"],
        "deposit_raw": transaction["deposit_raw"],
        "deposit": transaction["deposit"],
        "withdrawal_raw": transaction["withdrawal_raw"],
        "withdrawal": transaction["withdrawal"],
        "amount_raw": transaction["amount_raw"],
        "amount": transaction["amount"],
        "balance_raw": transaction["balance_raw"],
        "balance": transaction["balance"],
        "source_line": transaction["source_line"],
        "match_reasons": ["account", "amount"],
    }


def find_rv_uob_candidates(conn: Connection, voucher_number: str | None = None) -> list[dict[str, Any]]:
    uob_transactions = _fetch_uob_transactions(conn)
    results: list[dict[str, Any]] = []

    for bank_row in _fetch_rv_bank_rows(conn, voucher_number):
        if not allows_uob_reconciliation(bank_row.get("bank_code")):
            continue
        account = normalize_account(bank_row.get("bank_account_no"))
        amount = normalize_amount(bank_row.get("currency_amt"))
        candidates: list[dict[str, Any]] = []
        if account and amount is not None:
            for transaction in uob_transactions:
                tx_account = normalize_account(transaction.get("account_number"))
                tx_amount = normalize_amount(transaction.get("amount"))
                if tx_account == account and tx_amount is not None and abs(tx_amount) == abs(amount):
                    candidates.append(_candidate_payload(transaction))

        results.append(
            {
                "voucher_number": bank_row["voucher_number"],
                "rv_bank_row": {
                    "id": bank_row["bank_row_id"],
                    "row_order": bank_row["row_order"],
                    "bank_code": bank_row["bank_code"],
                    "bank_account_no": bank_row["bank_account_no"],
                    "check_no": bank_row["check_no"],
                    "bill_no": bank_row["bill_no"],
                    "amount_raw": bank_row["currency_amt_raw"],
                    "amount": bank_row["currency_amt"],
                },
                "status": _status_for_candidate_count(len(candidates)),
                "candidates": candidates,
            }
        )

    return results
