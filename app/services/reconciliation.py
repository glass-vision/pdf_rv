from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from sqlite3 import Connection
from typing import Any
from uuid import uuid4


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


def allows_kbank_reconciliation(bank_code: str | None) -> bool:
    normalized = normalize_bank_code(bank_code)
    if not normalized:
        return True
    return normalized in {"KBANK", "KASIKORN", "KASIKORNBANK"}


def normalize_check_no(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", "", str(value).strip().upper())
    normalized = re.sub(r"#\d+$", "", normalized)
    normalized = re.sub(r"-\d+$", "", normalized)
    return normalized or None


def _display_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 10 and text[4:5] == "-" and text[7:8] == "-":
        return f"{text[8:10]}/{text[5:7]}/{text[0:4]}"
    return text or None


def _status_for_candidate_count(count: int) -> str:
    if count == 0:
        return "no_match"
    if count == 1:
        return "unique_match"
    return "ambiguous_match"


def _format_amount(value: str | None) -> str | None:
    amount = normalize_amount(value)
    if amount is None:
        return None
    return format(amount, "f")


def _serialize_match_conditions(conditions: list[str] | None) -> str | None:
    if not conditions:
        return None
    return json.dumps([str(item) for item in conditions], ensure_ascii=False)


def _deserialize_match_conditions(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item)]
    text = str(parsed).strip()
    return [text] if text else []


def _confirmation_selection_source(
    confirmation: dict[str, Any] | None,
    *,
    auto_confirmed: bool = False,
) -> str | None:
    if confirmation and confirmation.get("selection_source"):
        return str(confirmation["selection_source"])
    if auto_confirmed:
        return "auto"
    if confirmation:
        return "manual"
    return None


def _condition(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"{label}={value}"


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
                check_refs.ref_values AS check_no_refs,
                br.bill_no,
                br.currency_amt_raw,
                br.currency_amt
            FROM receive_voucher_bank_rows br
            JOIN document_roots dr ON dr.id = br.document_root_id
            LEFT JOIN (
                SELECT document_root_id, GROUP_CONCAT(ref_value, ',') AS ref_values
                FROM document_refs
                WHERE ref_type = 'check_no'
                GROUP BY document_root_id
            ) check_refs ON check_refs.document_root_id = dr.id
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


def _fetch_confirmations(conn: Connection, bank_row_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not bank_row_ids:
        return {}
    placeholders = ",".join("?" for _ in bank_row_ids)
    rows = conn.execute(
        f"""
        SELECT
            rv_bank_row_id,
            uob_statement_key,
            uob_page_order,
            uob_row_order,
            match_rule,
            match_conditions,
            selection_source,
            confirmed_at
        FROM rv_uob_confirmations
        WHERE rv_bank_row_id IN ({placeholders})
        """,
        bank_row_ids,
    ).fetchall()
    return {row["rv_bank_row_id"]: dict(row) for row in rows}


def _fetch_auto_confirm_suppressions(conn: Connection, bank_row_ids: list[str]) -> set[str]:
    if not bank_row_ids:
        return set()
    placeholders = ",".join("?" for _ in bank_row_ids)
    rows = conn.execute(
        f"""
        SELECT rv_bank_row_id
        FROM rv_uob_auto_confirm_suppressions
        WHERE rv_bank_row_id IN ({placeholders})
        """,
        bank_row_ids,
    ).fetchall()
    return {row["rv_bank_row_id"] for row in rows}


def _candidate_payload(transaction: dict[str, Any]) -> dict[str, Any]:
    page_key = f"{transaction['statement_key']}:{transaction['page_order']}"
    match_rule = transaction.get("match_rule") or "account_amount"
    match_conditions = list(transaction.get("match_conditions") or [])
    return {
        "doc_type": "uob-page",
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
        "match_rule": match_rule,
        "match_conditions": match_conditions,
        "selection_source": None,
        "match_reasons": ["account", "amount"],
    }


def _candidate_matches_confirmation(candidate: dict[str, Any], confirmation: dict[str, Any]) -> bool:
    return (
        candidate.get("statement_key") == confirmation.get("uob_statement_key")
        and int(candidate.get("page_order") or 0) == int(confirmation.get("uob_page_order") or 0)
        and int(candidate.get("row_order") or 0) == int(confirmation.get("uob_row_order") or 0)
    )


def fetch_uob_transactions(conn: Connection) -> list[dict[str, Any]]:
    """Public entry point so batch callers can fetch once and reuse the
    result across multiple find_rv_uob_candidates calls, instead of each
    call re-scanning the whole uob_ca_statement_transactions table."""
    return _fetch_uob_transactions(conn)


def find_rv_uob_candidates(
    conn: Connection,
    voucher_number: str | None = None,
    uob_transactions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if uob_transactions is None:
        uob_transactions = _fetch_uob_transactions(conn)
    bank_rows = _fetch_rv_bank_rows(conn, voucher_number)
    confirmations = _fetch_confirmations(conn, [row["bank_row_id"] for row in bank_rows])
    suppressions = _fetch_auto_confirm_suppressions(conn, [row["bank_row_id"] for row in bank_rows])
    results: list[dict[str, Any]] = []

    for bank_row in bank_rows:
        if not allows_uob_reconciliation(bank_row.get("bank_code")):
            continue
        account = normalize_account(bank_row.get("bank_account_no"))
        amount = normalize_amount(bank_row.get("currency_amt"))
        match_conditions = [
            condition
            for condition in [
                _condition("account", account),
                _condition("amount", _format_amount(bank_row.get("currency_amt")) if amount is not None else None),
            ]
            if condition
        ]
        candidates: list[dict[str, Any]] = []
        if account and amount is not None:
            for transaction in uob_transactions:
                tx_account = normalize_account(transaction.get("account_number"))
                tx_amount = normalize_amount(transaction.get("amount"))
                if tx_account == account and tx_amount is not None and abs(tx_amount) == abs(amount):
                    candidates.append(
                        _candidate_payload(
                            {
                                **transaction,
                                "match_rule": "account_amount",
                                "match_conditions": match_conditions,
                            }
                        )
                    )
        status = _status_for_candidate_count(len(candidates))
        auto_confirmed = False
        confirmation = confirmations.get(bank_row["bank_row_id"])
        if (
            confirmation is None
            and status == "unique_match"
            and bank_row["bank_row_id"] not in suppressions
            and len(candidates) == 1
        ):
            # A single UOB candidate auto-confirms here, while the persisted
            # confirmation layer keeps the match reversible.
            candidate = candidates[0]
            confirmed_at = _store_rv_uob_confirmation(
                conn,
                bank_row_id=bank_row["bank_row_id"],
                statement_key=candidate["statement_key"],
                page_order=int(candidate["page_order"]),
                row_order=int(candidate["row_order"]),
                match_rule=candidate.get("match_rule"),
                match_conditions=candidate.get("match_conditions"),
                selection_source="auto",
                clear_auto_confirm_suppression=False,
            )
            confirmation = {
                "rv_bank_row_id": bank_row["bank_row_id"],
                "uob_statement_key": candidate["statement_key"],
                "uob_page_order": int(candidate["page_order"]),
                "uob_row_order": int(candidate["row_order"]),
                "match_rule": candidate.get("match_rule"),
                "match_conditions": _serialize_match_conditions(candidate.get("match_conditions")),
                "selection_source": "auto",
                "confirmed_at": confirmed_at,
            }
            auto_confirmed = True
        if confirmation:
            for candidate in candidates:
                if _candidate_matches_confirmation(candidate, confirmation):
                    candidate["confirmed"] = True
                    candidate["confirmed_at"] = confirmation.get("confirmed_at")
                    candidate["selection_source"] = _confirmation_selection_source(confirmation, auto_confirmed=auto_confirmed)
            status = "confirmed"

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
                "status": status,
                "confirmed": bool(confirmation),
                "confirmed_at": confirmation.get("confirmed_at") if confirmation else None,
                "auto_confirmed": auto_confirmed,
                "auto_confirm_suppressed": bank_row["bank_row_id"] in suppressions,
                "candidates": candidates,
                "match_rule": candidates[0].get("match_rule") if candidates else "account_amount",
                "match_conditions": match_conditions,
                "selection_source": _confirmation_selection_source(confirmation, auto_confirmed=auto_confirmed),
            }
        )

    return results


def _find_candidate_for_bank_row(
    conn: Connection,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT dr.root_key AS voucher_number
        FROM receive_voucher_bank_rows br
        JOIN document_roots dr ON dr.id = br.document_root_id
        WHERE br.id = ?
        """,
        (bank_row_id,),
    ).fetchone()
    if row is None:
        return None
    for result in find_rv_uob_candidates(conn, voucher_number=row["voucher_number"]):
        if result["rv_bank_row"]["id"] != bank_row_id:
            continue
        for candidate in result["candidates"]:
            if (
                candidate["statement_key"] == statement_key
                and int(candidate["page_order"]) == int(page_order)
                and int(candidate["row_order"]) == int(row_order)
            ):
                return candidate
    return None


def _store_rv_uob_confirmation(
    conn: Connection,
    *,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
    match_rule: str | None = None,
    match_conditions: list[str] | None = None,
    selection_source: str | None = None,
    clear_auto_confirm_suppression: bool = True,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    confirmation_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO rv_uob_confirmations (
            id, rv_bank_row_id, uob_statement_key, uob_page_order, uob_row_order,
            match_rule, match_conditions, selection_source, confirmed_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rv_bank_row_id) DO UPDATE SET
            uob_statement_key = excluded.uob_statement_key,
            uob_page_order = excluded.uob_page_order,
            uob_row_order = excluded.uob_row_order,
            match_rule = excluded.match_rule,
            match_conditions = excluded.match_conditions,
            selection_source = excluded.selection_source,
            confirmed_at = excluded.confirmed_at
        """,
        (
            confirmation_id,
            bank_row_id,
            statement_key,
            int(page_order),
            int(row_order),
            match_rule,
            _serialize_match_conditions(match_conditions),
            selection_source,
            now,
            now,
        ),
    )
    if clear_auto_confirm_suppression:
        conn.execute(
            "DELETE FROM rv_uob_auto_confirm_suppressions WHERE rv_bank_row_id = ?",
            (bank_row_id,),
        )
    conn.commit()
    return now


def confirm_rv_uob_candidate(
    conn: Connection,
    *,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
    match_rule: str | None = None,
    match_conditions: list[str] | None = None,
    selection_source: str = "manual",
    clear_auto_confirm_suppression: bool = True,
) -> dict[str, Any]:
    candidate = _find_candidate_for_bank_row(conn, bank_row_id, statement_key, page_order, row_order)
    if candidate is None:
        raise ValueError("UOB candidate does not match this Receive Voucher bank row")
    now = _store_rv_uob_confirmation(
        conn,
        bank_row_id=bank_row_id,
        statement_key=statement_key,
        page_order=page_order,
        row_order=row_order,
        match_rule=match_rule or candidate.get("match_rule"),
        match_conditions=match_conditions or candidate.get("match_conditions"),
        selection_source=selection_source,
        clear_auto_confirm_suppression=clear_auto_confirm_suppression,
    )
    candidate["confirmed"] = True
    candidate["confirmed_at"] = now
    candidate["selection_source"] = selection_source
    return {
        "bank_row_id": bank_row_id,
        "status": "confirmed",
        "confirmed_at": now,
        "candidate": candidate,
    }


def unconfirm_rv_uob_candidate(conn: Connection, *, bank_row_id: str) -> dict[str, Any]:
    existing = conn.execute(
        """
        SELECT rv_bank_row_id, confirmed_at
        FROM rv_uob_confirmations
        WHERE rv_bank_row_id = ?
        """,
        (bank_row_id,),
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM rv_uob_confirmations WHERE rv_bank_row_id = ?", (bank_row_id,))
    conn.execute(
        """
        INSERT INTO rv_uob_auto_confirm_suppressions (
            rv_bank_row_id, suppressed_at, created_at
        )
        VALUES (?, ?, ?)
        ON CONFLICT(rv_bank_row_id) DO UPDATE SET
            suppressed_at = excluded.suppressed_at
        """,
        (bank_row_id, now, now),
    )
    conn.commit()
    return {
        "bank_row_id": bank_row_id,
        "status": "unconfirmed",
        "was_confirmed": existing is not None,
        "suppressed_at": now,
    }


def confirm_unique_rv_uob_candidates(conn: Connection, *, voucher_number: str) -> dict[str, Any]:
    confirmed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for result in find_rv_uob_candidates(conn, voucher_number=voucher_number):
        if result.get("status") == "confirmed":
            skipped.append({"bank_row_id": result["rv_bank_row"]["id"], "reason": "already_confirmed"})
            continue
        candidates = result.get("candidates") or []
        if result.get("status") != "unique_match" or len(candidates) != 1:
            skipped.append({"bank_row_id": result["rv_bank_row"]["id"], "reason": result.get("status")})
            continue
        candidate = candidates[0]
        confirmed.append(
            confirm_rv_uob_candidate(
                conn,
                bank_row_id=result["rv_bank_row"]["id"],
                statement_key=candidate["statement_key"],
                page_order=int(candidate["page_order"]),
                row_order=int(candidate["row_order"]),
                match_rule=candidate.get("match_rule"),
                match_conditions=candidate.get("match_conditions"),
                selection_source="auto",
                clear_auto_confirm_suppression=False,
            )
        )
    return {
        "voucher_number": voucher_number,
        "confirmed_count": len(confirmed),
        "confirmed": confirmed,
        "skipped": skipped,
    }


def fetch_kbank_deposit_rows(
    conn: Connection,
    normalized_checks: list[str] | None = None,
    normalized_amounts: list[str] | None = None,
) -> list[dict[str, Any]]:
    params: list[str] = []
    filters: list[str] = []
    if normalized_checks is not None:
        if normalized_checks:
            placeholders = ",".join("?" for _ in normalized_checks)
            filters.append(f"kcr.check_no IN ({placeholders})")
            params.extend(normalized_checks)
    if normalized_amounts is not None:
        if normalized_amounts:
            placeholders = ",".join("?" for _ in normalized_amounts)
            filters.append(f"kcr.amount IN ({placeholders})")
            params.extend(normalized_amounts)
    if normalized_checks is not None and not normalized_checks and (normalized_amounts is None or not normalized_amounts):
        return []
    if normalized_amounts is not None and not normalized_amounts and (normalized_checks is None or not normalized_checks):
        return []
    where_filter = ""
    if filters:
        where_filter = " AND (" + " OR ".join(filters) + ")"
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                kcr.id AS id,
                kcr.check_no AS normalized_check_no,
                kcr.check_no AS check_no,
                kcr.row_order,
                kcr.amount AS amount,
                kcr.amount_raw AS amount_raw,
                kcr.source_line,
                dp.page_order,
                dr.root_key AS statement_reference,
                dr.root_date,
                dr.name AS account_name,
                dr.customer_code AS account_number
            FROM kbank_check_rows kcr
            JOIN document_roots dr ON dr.id = kcr.document_root_id
            JOIN document_pages dp
              ON dp.document_root_id = dr.id AND dp.page_order = kcr.page_order
            WHERE dr.doc_type = 'kbank-statements'
              AND kcr.event_type = 'deposit'
              {where_filter}
            ORDER BY dr.root_date DESC, dr.root_key, dp.page_order
            """,
            params,
        ).fetchall()
    ]
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row.get("id") or "")
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        deduped.append(row)
    return deduped


def _kbank_deposit_rows_by_check(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_check: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_check.setdefault(str(row["normalized_check_no"]), []).append(row)
    return by_check


def _kbank_deposit_rows_by_amount(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_amount: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        amount = _format_amount(row.get("amount"))
        if amount is None:
            continue
        by_amount.setdefault(amount, []).append(row)
    return by_amount


def _normalized_check_candidates(bank_row: dict[str, Any]) -> list[str]:
    raw_values = [bank_row.get("check_no")]
    raw_values.extend(str(bank_row.get("check_no_refs") or "").split(","))
    checks: list[str] = []
    for raw_value in raw_values:
        normalized = normalize_check_no(raw_value)
        if normalized and normalized not in checks:
            checks.append(normalized)
    return checks


def _candidate_payload_kbank(
    row: dict[str, Any],
    *,
    match_rule: str,
    match_conditions: list[str],
) -> dict[str, Any]:
    statement_key = row["statement_reference"]
    page_key = f"{statement_key}:{row['page_order']}"
    source_line = str(row.get("source_line") or "")
    # A bounced check's return event and its later redeposit can print the
    # identical check number and amount on the same statement page. The
    # source line's own date/time prefix (e.g. "14-08-25 18:31") is unique
    # per event and lets the PDF highlighter isolate the right printed row -
    # see app/services/pdf_highlighter.py's anchored-window clustering.
    date_time_tokens = source_line.split(maxsplit=2)[:2]
    date_time_term = " ".join(date_time_tokens) if len(date_time_tokens) == 2 else None
    return {
        "doc_type": "kbank-page",
        "statement_key": statement_key,
        "statement_reference": statement_key,
        "key": page_key,
        "page_key": page_key,
        "page_order": row["page_order"],
        "assembled_page_count": 1,
        "pdf_url": f"/api/kbank-statements/{statement_key}.pdf",
        "page_pdf_url": f"/api/kbank-statements/pages/{page_key}.pdf",
        "name": row.get("account_name"),
        "customer_code": row.get("account_number"),
        "date": _display_date(row.get("root_date")),
        "row_order": row.get("row_order"),
        "amount": row.get("amount"),
        "amount_raw": row.get("amount_raw"),
        "matched_check_no": row.get("check_no"),
        "normalized_check_no": row.get("normalized_check_no"),
        "source_line": source_line,
        "match_rule": match_rule,
        "match_conditions": match_conditions,
        "selection_source": None,
        "highlight_terms": [
            row.get("normalized_check_no"),
            row.get("check_no"),
            row.get("amount_raw"),
            row.get("amount"),
            *([date_time_term] if date_time_term else []),
        ],
        "match_reasons": [match_rule],
    }


def _candidate_matches_kbank_confirmation(candidate: dict[str, Any], confirmation: dict[str, Any]) -> bool:
    return (
        candidate.get("statement_key") == confirmation.get("kbank_statement_key")
        and int(candidate.get("page_order") or 0) == int(confirmation.get("kbank_page_order") or 0)
        and int(candidate.get("row_order") or 0) == int(confirmation.get("kbank_row_order") or 0)
    )


def _fetch_kbank_confirmations(conn: Connection, bank_row_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not bank_row_ids:
        return {}
    placeholders = ",".join("?" for _ in bank_row_ids)
    rows = conn.execute(
        f"""
        SELECT
            rv_bank_row_id,
            kbank_statement_key,
            kbank_page_order,
            kbank_row_order,
            match_rule,
            match_conditions,
            selection_source,
            confirmed_at
        FROM rv_kbank_confirmations
        WHERE rv_bank_row_id IN ({placeholders})
        """,
        bank_row_ids,
    ).fetchall()
    return {row["rv_bank_row_id"]: dict(row) for row in rows}


def _fetch_kbank_auto_confirm_suppressions(conn: Connection, bank_row_ids: list[str]) -> set[str]:
    if not bank_row_ids:
        return set()
    placeholders = ",".join("?" for _ in bank_row_ids)
    rows = conn.execute(
        f"""
        SELECT rv_bank_row_id
        FROM rv_kbank_auto_confirm_suppressions
        WHERE rv_bank_row_id IN ({placeholders})
        """,
        bank_row_ids,
    ).fetchall()
    return {row["rv_bank_row_id"] for row in rows}


def find_rv_kbank_candidates(
    conn: Connection,
    voucher_number: str | None = None,
    kbank_deposit_rows_by_check: dict[str, list[dict[str, Any]]] | None = None,
    kbank_deposit_rows_by_amount: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    bank_rows = _fetch_rv_bank_rows(conn, voucher_number)
    confirmations = _fetch_kbank_confirmations(conn, [row["bank_row_id"] for row in bank_rows])
    suppressions = _fetch_kbank_auto_confirm_suppressions(conn, [row["bank_row_id"] for row in bank_rows])
    if kbank_deposit_rows_by_check is None or kbank_deposit_rows_by_amount is None:
        all_checks = sorted({check for row in bank_rows for check in _normalized_check_candidates(row)})
        all_amounts = sorted(
            {
                amount
                for row in bank_rows
                if (amount := _format_amount(row.get("currency_amt")))
            }
        )
        fetched = fetch_kbank_deposit_rows(conn, all_checks or None, all_amounts or None)
        if kbank_deposit_rows_by_check is None:
            kbank_deposit_rows_by_check = _kbank_deposit_rows_by_check(fetched)
        if kbank_deposit_rows_by_amount is None:
            kbank_deposit_rows_by_amount = _kbank_deposit_rows_by_amount(fetched)
    results: list[dict[str, Any]] = []

    for bank_row in bank_rows:
        if not allows_kbank_reconciliation(bank_row.get("bank_code")):
            continue
        bank_amount = _format_amount(bank_row.get("currency_amt"))
        candidates: list[dict[str, Any]] = []
        for normalized_check in _normalized_check_candidates(bank_row):
            for row in kbank_deposit_rows_by_check.get(normalized_check, []):
                candidates.append(
                    _candidate_payload_kbank(
                        row,
                        match_rule="check_no",
                        match_conditions=[_condition("check_no", row.get("normalized_check_no")) or "check_no"],
                    )
                )
        if not candidates and bank_amount:
            for row in kbank_deposit_rows_by_amount.get(bank_amount, []):
                candidates.append(
                    _candidate_payload_kbank(
                        row,
                        match_rule="amount",
                        match_conditions=[_condition("amount", row.get("amount")) or "amount"],
                    )
                )
        status = _status_for_candidate_count(len(candidates))
        auto_confirmed = False
        confirmation = confirmations.get(bank_row["bank_row_id"])
        if (
            confirmation is None
            and status == "unique_match"
            and bank_row["bank_row_id"] not in suppressions
            and len(candidates) == 1
        ):
            # A single KBank candidate auto-confirms here, while the
            # persisted confirmation layer keeps the match reversible.
            candidate = candidates[0]
            confirmed_at = _store_rv_kbank_confirmation(
                conn,
                bank_row_id=bank_row["bank_row_id"],
                statement_key=candidate["statement_key"],
                page_order=int(candidate["page_order"]),
                row_order=int(candidate["row_order"]),
                match_rule=candidate.get("match_rule"),
                match_conditions=candidate.get("match_conditions"),
                selection_source="auto",
                clear_auto_confirm_suppression=False,
            )
            confirmation = {
                "rv_bank_row_id": bank_row["bank_row_id"],
                "kbank_statement_key": candidate["statement_key"],
                "kbank_page_order": int(candidate["page_order"]),
                "kbank_row_order": int(candidate["row_order"]),
                "match_rule": candidate.get("match_rule"),
                "match_conditions": _serialize_match_conditions(candidate.get("match_conditions")),
                "selection_source": "auto",
                "confirmed_at": confirmed_at,
            }
            auto_confirmed = True
        if confirmation:
            for candidate in candidates:
                if _candidate_matches_kbank_confirmation(candidate, confirmation):
                    candidate["confirmed"] = True
                    candidate["confirmed_at"] = confirmation.get("confirmed_at")
                    candidate["selection_source"] = _confirmation_selection_source(confirmation, auto_confirmed=auto_confirmed)
            status = "confirmed"

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
                "status": status,
                "confirmed": bool(confirmation),
                "confirmed_at": confirmation.get("confirmed_at") if confirmation else None,
                "auto_confirmed": auto_confirmed,
                "auto_confirm_suppressed": bank_row["bank_row_id"] in suppressions,
                "candidates": candidates,
                "match_rule": candidates[0].get("match_rule") if candidates else None,
                "match_conditions": candidates[0].get("match_conditions") if candidates else [],
                "selection_source": _confirmation_selection_source(confirmation, auto_confirmed=auto_confirmed),
            }
        )

    return results


def _find_candidate_for_kbank_bank_row(
    conn: Connection,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT dr.root_key AS voucher_number
        FROM receive_voucher_bank_rows br
        JOIN document_roots dr ON dr.id = br.document_root_id
        WHERE br.id = ?
        """,
        (bank_row_id,),
    ).fetchone()
    if row is None:
        return None
    for result in find_rv_kbank_candidates(conn, voucher_number=row["voucher_number"]):
        if result["rv_bank_row"]["id"] != bank_row_id:
            continue
        for candidate in result["candidates"]:
            if (
                candidate["statement_key"] == statement_key
                and int(candidate["page_order"]) == int(page_order)
                and int(candidate["row_order"]) == int(row_order)
            ):
                return candidate
    return None


def _store_rv_kbank_confirmation(
    conn: Connection,
    *,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
    match_rule: str | None = None,
    match_conditions: list[str] | None = None,
    selection_source: str | None = None,
    clear_auto_confirm_suppression: bool = True,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    confirmation_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO rv_kbank_confirmations (
            id, rv_bank_row_id, kbank_statement_key, kbank_page_order, kbank_row_order,
            match_rule, match_conditions, selection_source, confirmed_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rv_bank_row_id) DO UPDATE SET
            kbank_statement_key = excluded.kbank_statement_key,
            kbank_page_order = excluded.kbank_page_order,
            kbank_row_order = excluded.kbank_row_order,
            match_rule = excluded.match_rule,
            match_conditions = excluded.match_conditions,
            selection_source = excluded.selection_source,
            confirmed_at = excluded.confirmed_at
        """,
        (
            confirmation_id,
            bank_row_id,
            statement_key,
            int(page_order),
            int(row_order),
            match_rule,
            _serialize_match_conditions(match_conditions),
            selection_source,
            now,
            now,
        ),
    )
    if clear_auto_confirm_suppression:
        conn.execute(
            "DELETE FROM rv_kbank_auto_confirm_suppressions WHERE rv_bank_row_id = ?",
            (bank_row_id,),
        )
    conn.commit()
    return now


def confirm_rv_kbank_candidate(
    conn: Connection,
    *,
    bank_row_id: str,
    statement_key: str,
    page_order: int,
    row_order: int,
    match_rule: str | None = None,
    match_conditions: list[str] | None = None,
    selection_source: str = "manual",
    clear_auto_confirm_suppression: bool = True,
) -> dict[str, Any]:
    candidate = _find_candidate_for_kbank_bank_row(conn, bank_row_id, statement_key, page_order, row_order)
    if candidate is None:
        raise ValueError("KBank candidate does not match this Receive Voucher bank row")
    now = _store_rv_kbank_confirmation(
        conn,
        bank_row_id=bank_row_id,
        statement_key=statement_key,
        page_order=page_order,
        row_order=row_order,
        match_rule=match_rule or candidate.get("match_rule"),
        match_conditions=match_conditions or candidate.get("match_conditions"),
        selection_source=selection_source,
        clear_auto_confirm_suppression=clear_auto_confirm_suppression,
    )
    candidate["confirmed"] = True
    candidate["confirmed_at"] = now
    candidate["selection_source"] = selection_source
    return {
        "bank_row_id": bank_row_id,
        "status": "confirmed",
        "confirmed_at": now,
        "candidate": candidate,
    }


def unconfirm_rv_kbank_candidate(conn: Connection, *, bank_row_id: str) -> dict[str, Any]:
    existing = conn.execute(
        """
        SELECT rv_bank_row_id, confirmed_at
        FROM rv_kbank_confirmations
        WHERE rv_bank_row_id = ?
        """,
        (bank_row_id,),
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM rv_kbank_confirmations WHERE rv_bank_row_id = ?", (bank_row_id,))
    conn.execute(
        """
        INSERT INTO rv_kbank_auto_confirm_suppressions (
            rv_bank_row_id, suppressed_at, created_at
        )
        VALUES (?, ?, ?)
        ON CONFLICT(rv_bank_row_id) DO UPDATE SET
            suppressed_at = excluded.suppressed_at
        """,
        (bank_row_id, now, now),
    )
    conn.commit()
    return {
        "bank_row_id": bank_row_id,
        "status": "unconfirmed",
        "was_confirmed": existing is not None,
        "suppressed_at": now,
    }


def confirm_unique_rv_kbank_candidates(conn: Connection, *, voucher_number: str) -> dict[str, Any]:
    confirmed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for result in find_rv_kbank_candidates(conn, voucher_number=voucher_number):
        if result.get("status") == "confirmed":
            skipped.append({"bank_row_id": result["rv_bank_row"]["id"], "reason": "already_confirmed"})
            continue
        candidates = result.get("candidates") or []
        if result.get("status") != "unique_match" or len(candidates) != 1:
            skipped.append({"bank_row_id": result["rv_bank_row"]["id"], "reason": result.get("status")})
            continue
        candidate = candidates[0]
        confirmed.append(
            confirm_rv_kbank_candidate(
                conn,
                bank_row_id=result["rv_bank_row"]["id"],
                statement_key=candidate["statement_key"],
                page_order=int(candidate["page_order"]),
                row_order=int(candidate["row_order"]),
                match_rule=candidate.get("match_rule"),
                match_conditions=candidate.get("match_conditions"),
                selection_source="auto",
                clear_auto_confirm_suppression=False,
            )
        )
    return {
        "voucher_number": voucher_number,
        "confirmed_count": len(confirmed),
        "confirmed": confirmed,
        "skipped": skipped,
    }
