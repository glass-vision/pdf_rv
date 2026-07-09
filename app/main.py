from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ensure_data_dirs, get_settings
from app.database import init_db
from app.routers.credit_memos import router as credit_memo_router
from app.routers.invoices import router as invoice_router
from app.routers.kbank_statements import router as kbank_statement_router
from app.routers.reconciliation import router as reconciliation_router
from app.routers.receive_vouchers import (
    get_receive_voucher_pdf,
    router as receive_voucher_router,
    search_receive_vouchers,
)
from app.routers.uob_ca_statements import router as uob_ca_statement_router
from app.database import db_session
from app.services.pdf_assembler import assemble_pdf
from app.services.pdf_highlighter import highlight_pdf_row
from app.services.document_core import fetch_document_page_by_order, fetch_document_root
from app.services.storage import read_pdf_from_storage
from app.services.upload_worker import start_upload_worker, stop_upload_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_dirs()
    init_db()
    settings = get_settings()
    start_upload_worker(settings.upload_worker_count)
    yield
    stop_upload_worker()


app = FastAPI(title="PDF Receive Voucher MVP", version="0.1.0", lifespan=lifespan)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(receive_voucher_router)
app.include_router(invoice_router)
app.include_router(credit_memo_router)
app.include_router(kbank_statement_router)
app.include_router(uob_ca_statement_router)
app.include_router(reconciliation_router)


STATIC_DIR = Path(__file__).resolve().parent / "static"


def get_static_version() -> int:
    try:
        return int(max(path.stat().st_mtime for path in STATIC_DIR.glob("*") if path.is_file()))
    except ValueError:
        return 0


DOC_TYPES = [
    {
        "key": "receive-vouchers",
        "label": "Receive Vouchers",
        "url": "/receive-vouchers",
        "api": "/api/workspace/quick-access/receive-vouchers",
        "search_label": "Voucher Number",
        "search_placeholder": "ค้นหา Voucher Number… เช่น QR2508-00001",
    },
    {
        "key": "invoices",
        "label": "Invoices",
        "url": "/invoices",
        "api": "/api/workspace/quick-access/invoices",
        "search_label": "Invoice Number",
        "search_placeholder": "ค้นหา Invoice Number… เช่น ST2505-0001",
    },
    {
        "key": "credit-memos",
        "label": "Credit Memos",
        "url": "/credit-memos",
        "api": "/api/workspace/quick-access/credit-memos",
        "search_label": "Credit Memo Number",
        "search_placeholder": "ค้นหา Credit Memo Number… เช่น CZ2506-0001",
    },
    {
        "key": "kbank-statements",
        "label": "KBank Statements",
        "url": "/kbank-statements",
        "api": "/api/workspace/quick-access/kbank-statements",
        "search_label": "Statement Reference",
        "search_placeholder": "ค้นหา Statement Reference… เช่น KB2025-001",
    },
    {
        "key": "uob-ca-statements",
        "label": "UOB Current Account Statements",
        "url": "/uob-ca-statements",
        "api": "/api/workspace/quick-access/uob-ca-statements",
        "search_label": "Statement Key",
        "search_placeholder": "ค้นหา Statement Key… เช่น UOB-CA-XXXXXXXXXX",
    },
]
DOC_TYPE_INDEX = {doc["key"]: doc for doc in DOC_TYPES}


def _display_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 10 and text[4:5] == "-" and text[7:8] == "-":
        return f"{text[8:10]}/{text[5:7]}/{text[0:4]}"
    return text or None


def _normalize_chain_node(
    *,
    node_id: str,
    doc_type: str,
    key_value: str,
    date_field: str | None,
    name: str | None,
    customer_code: str | None,
    assembled_page_count: int | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node_id,
        "doc_type": doc_type,
        "key": key_value,
        "name": name,
        "customer_code": customer_code,
        "assembled_page_count": assembled_page_count or 0,
        "pdf_url": f"/api/{doc_type}/{key_value}.pdf",
    }
    if date_field is not None:
        payload["date"] = _display_date(date_field)
    return payload


_DOC_TYPE_PDF_PREFIX = {
    "receive-vouchers": "/api/receive-vouchers",
    "invoices": "/api/invoices",
    "credit-memos": "/api/credit-memos",
    "kbank-statements": "/api/kbank-statements",
    "uob-ca-statements": "/api/uob-ca-statements",
}


def _load_quick_access_rows(doc_type: str, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Quick Access search requires a filter")
    if doc_type not in _DOC_TYPE_PDF_PREFIX:
        return []
    normalized_limit = max(1, min(limit, 30))
    like = f"%{normalized_query}%"
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, root_key, display_key, root_date, name, customer_code,
                   assembled_page_count, updated_at
            FROM document_roots
            WHERE doc_type = ?
              AND (root_key LIKE ? OR name LIKE ? OR customer_code LIKE ?)
            ORDER BY root_date DESC, root_key DESC
            LIMIT ?
            """,
            (doc_type, like, like, like, normalized_limit),
        ).fetchall()

    prefix = _DOC_TYPE_PDF_PREFIX[doc_type]
    return [
        {
            "id": row["id"],
            "doc_type": doc_type,
            "root_key": row["root_key"],
            "display_key": row["display_key"] or row["root_key"],
            "root_date": _display_date(row["root_date"]),
            "name": row["name"],
            "customer_code": row["customer_code"],
            "detail": row["customer_code"],
            "assembled_page_count": int(row["assembled_page_count"] or 0),
            "updated_at": row["updated_at"],
            "pdf_url": f"{prefix}/{row['root_key']}.pdf",
            "refs": {},
        }
        for row in rows
    ]


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _normalize_bridge_entry(row: dict[str, Any], invoice_row: dict[str, Any] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ref_value": row["invoice_ref_value"],
        "normalized_value": row["invoice_number"],
        "resolved": invoice_row is not None,
    }
    if invoice_row is not None:
        entry["invoice"] = invoice_row
    return entry


def _read_chain_pdf(conn, doc_type: str, node_id: str) -> bytes:
    row = conn.execute(
        """
        SELECT assembled_pdf, assembled_pdf_path
        FROM document_roots
        WHERE id = ?
        """,
        (node_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"{doc_type} row not found for {node_id}")
    return read_pdf_from_storage(row["assembled_pdf"], row["assembled_pdf_path"])


def _read_statement_page_pdf(conn, doc_type: str, root_key: str, page_order: int) -> bytes:
    root = fetch_document_root(conn, doc_type, root_key)
    if not root:
        raise LookupError(f"{doc_type} root not found for {root_key}")
    page = fetch_document_page_by_order(conn, root["id"], page_order)
    if not page:
        raise LookupError(f"{doc_type} page not found for {root_key}:{page_order}")
    return read_pdf_from_storage(page["page_pdf"], page["page_pdf_path"])


def _build_mapping_summary(root_payload: dict[str, Any]) -> dict[str, Any]:
    invoice_bridges = list(root_payload.get("invoice_bridges") or [])
    unresolved_invoice_refs = list(root_payload.get("unresolved_invoice_refs") or [])
    kbank_candidate_bridges = list(root_payload.get("kbank_candidate_bridges") or [])
    uob_candidate_bridges = list(root_payload.get("uob_candidate_bridges") or [])
    resolved_invoices = sum(1 for bridge in invoice_bridges if bridge.get("resolved") and bridge.get("invoice"))
    invoice_refs = len(invoice_bridges) + len(unresolved_invoice_refs)
    invoice_complete = invoice_refs > 0 and not unresolved_invoice_refs
    credit_memo_count = sum(
        len((bridge.get("invoice") or {}).get("credit_memos") or [])
        for bridge in invoice_bridges
        if bridge.get("resolved") and bridge.get("invoice")
    )
    kbank_candidates = sum(len(item.get("candidates") or []) for item in kbank_candidate_bridges)
    kbank_confirmed = sum(1 for item in kbank_candidate_bridges if item.get("status") == "confirmed")
    uob_candidates = sum(len(item.get("candidates") or []) for item in uob_candidate_bridges)
    uob_confirmed = sum(1 for item in uob_candidate_bridges if item.get("status") == "confirmed")
    # `bank_pages` counts confirmed bank evidence only (KBank check_no matches
    # and UOB account+amount matches both go through the same candidate/
    # confirm flow - ambiguous matches require a manual pick, matching
    # candidates alone do not mark the Bank branch complete).
    bank_pages = kbank_confirmed
    bank_candidates = len(uob_candidate_bridges) + len(kbank_candidate_bridges)
    bank_complete = bank_pages > 0 or uob_confirmed > 0
    overall_complete = invoice_complete and bank_complete
    return {
        "invoice_refs": invoice_refs,
        "resolved_invoices": resolved_invoices,
        "unresolved_invoices": len(unresolved_invoice_refs),
        "credit_memo_count": credit_memo_count,
        "bank_pages": bank_pages,
        "bank_candidates": bank_candidates,
        "kbank_candidate_groups": len(kbank_candidate_bridges),
        "kbank_candidates": kbank_candidates,
        "kbank_confirmed": kbank_confirmed,
        "uob_candidate_groups": len(uob_candidate_bridges),
        "uob_candidates": uob_candidates,
        "uob_confirmed": uob_confirmed,
        "invoice_complete": invoice_complete,
        "bank_complete": bank_complete,
        "overall_complete": overall_complete,
    }


def _build_chain_json_view(chains: list[dict[str, Any]]) -> dict[str, Any]:
    complete: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    candidate_bank_statements: list[dict[str, Any]] = []

    for chain in chains:
        status = chain.get("mapping_status") or (
            "complete" if chain.get("mapping_summary", {}).get("overall_complete") else "incomplete"
        )
        kbank_candidate_bridges = list(chain.get("kbank_candidate_bridges") or [])
        uob_candidate_bridges = list(chain.get("uob_candidate_bridges") or [])
        kbank_candidates = sum(len(item.get("candidates") or []) for item in kbank_candidate_bridges)
        uob_candidates = sum(len(item.get("candidates") or []) for item in uob_candidate_bridges)
        candidate_choices = kbank_candidates + uob_candidates
        bank_candidates = len(kbank_candidate_bridges) + len(uob_candidate_bridges)
        if candidate_choices > 1:
            candidate_bank_statements.append(
                {
                    "voucher_number": chain.get("voucher_number") or chain.get("key") or "",
                    "status": status,
                    "bank_candidates": bank_candidates,
                    "candidate_choices": candidate_choices,
                    "kbank_candidates": kbank_candidates,
                    "uob_candidates": uob_candidates,
                    "bank_complete": bool(chain.get("mapping_summary", {}).get("bank_complete")),
                }
            )
        if status == "complete":
            complete.append(chain)
        else:
            incomplete.append(chain)

    total = len(chains)
    complete_count = len(complete)
    incomplete_count = len(incomplete)
    return {
        "summary": {
            "total": total,
            "complete": complete_count,
            "incomplete": incomplete_count,
            "candidate_bank_statements": len(candidate_bank_statements),
            "completion_rate": total and round((complete_count / total) * 100, 2) or 0,
        },
        "complete": complete,
        "incomplete": incomplete,
        "candidate_bank_statements": candidate_bank_statements,
    }


def _confirmed_uob_page_refs(root_payload: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bridge in root_payload.get("uob_candidate_bridges") or []:
        for candidate in bridge.get("candidates") or []:
            if not candidate.get("confirmed"):
                continue
            statement_key = candidate.get("statement_key")
            page_order = candidate.get("page_order")
            if not statement_key or not page_order:
                continue
            page_key = candidate.get("page_key") or candidate.get("key") or f"{statement_key}:{page_order}"
            if page_key in seen:
                continue
            seen.add(page_key)
            pages.append(
                {
                    "statement_key": statement_key,
                    "page_order": int(page_order),
                    "page_key": page_key,
                    "row_order": int(candidate.get("row_order") or 1),
                    "highlight_terms": [
                        candidate.get("customer_ref"),
                        candidate.get("transaction_ref"),
                        candidate.get("transaction_id"),
                        candidate.get("amount_raw"),
                        candidate.get("amount"),
                    ],
                }
            )
    return pages


def _confirmed_kbank_page_refs(root_payload: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bridge in root_payload.get("kbank_candidate_bridges") or []:
        for candidate in bridge.get("candidates") or []:
            if not candidate.get("confirmed"):
                continue
            statement_key = candidate.get("statement_key")
            page_order = candidate.get("page_order")
            if not statement_key or not page_order:
                continue
            page_key = candidate.get("page_key") or candidate.get("key") or f"{statement_key}:{page_order}"
            if page_key in seen:
                continue
            seen.add(page_key)
            pages.append(
                {
                    "statement_key": statement_key,
                    "statement_reference": statement_key,
                    "page_order": int(page_order),
                    "page_key": page_key,
                    "row_order": int(candidate.get("row_order") or 1),
                    "highlight_terms": candidate.get("highlight_terms") or [],
                }
            )
    return pages


def _load_workspace_chain(
    *,
    voucher_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
) -> list[dict[str, Any]]:
    roots = search_receive_vouchers(
        voucher_number=voucher_number,
        date_from=date_from,
        date_to=date_to,
        ref_type=ref_type,
        ref_value=ref_value,
    )
    if not roots:
        return []

    root_ids = [str(row["id"]) for row in roots if row.get("id")]
    if not root_ids:
        return []

    root_payloads = {
        root["id"]: {
            **root,
            "doc_type": "receive-vouchers",
            "pdf_url": f"/api/receive-vouchers/{root['voucher_number']}.pdf",
            "chain_pdf_url": f"/api/receive-vouchers/{root['voucher_number']}-chain.pdf",
            "invoice_bridges": [],
            "uob_candidate_bridges": [
                {
                    "rv_bank_row": bank_row.get("uob_match", {}).get("rv_bank_row"),
                    "status": bank_row.get("uob_match", {}).get("status"),
                    "match_rule": bank_row.get("uob_match", {}).get("match_rule"),
                    "match_conditions": bank_row.get("uob_match", {}).get("match_conditions") or [],
                    "selection_source": bank_row.get("uob_match", {}).get("selection_source"),
                    "candidates": bank_row.get("uob_match", {}).get("candidates") or [],
                }
                for bank_row in (root.get("bank_rows") or [])
                if bank_row.get("uob_match") and bank_row.get("uob_match", {}).get("candidates")
            ],
            "kbank_candidate_bridges": [
                {
                    "rv_bank_row": bank_row.get("kbank_match", {}).get("rv_bank_row"),
                    "status": bank_row.get("kbank_match", {}).get("status"),
                    "match_rule": bank_row.get("kbank_match", {}).get("match_rule"),
                    "match_conditions": bank_row.get("kbank_match", {}).get("match_conditions") or [],
                    "selection_source": bank_row.get("kbank_match", {}).get("selection_source"),
                    "candidates": bank_row.get("kbank_match", {}).get("candidates") or [],
                }
                for bank_row in (root.get("bank_rows") or [])
                if bank_row.get("kbank_match") and bank_row.get("kbank_match", {}).get("candidates")
            ],
            "unresolved_invoice_refs": [],
            "invoices": [],
            "chain_page_count": int(root.get("assembled_page_count") or 0),
        }
        for root in roots
    }

    with db_session() as conn:
        invoice_links = conn.execute(
            f"""
            SELECT DISTINCT
                rv.id AS root_id,
                rr.normalized_value AS invoice_number,
                rr.ref_value AS invoice_ref_value,
                rr.created_at AS created_at
            FROM document_refs rr
            JOIN document_roots rv ON rv.id = rr.document_root_id
            WHERE rr.ref_type = 'invoice_no'
              AND rv.doc_type = 'receive-vouchers'
              AND rv.id IN ({_placeholders(len(root_ids))})
            ORDER BY rv.root_key, rr.created_at, rr.ref_value
            """,
            root_ids,
        ).fetchall()

        invoices_by_number = {}
        invoice_numbers = []
        for link in invoice_links:
            normalized = link["invoice_number"]
            if normalized not in invoices_by_number:
                invoice_numbers.append(normalized)

        invoice_rows = []
        if invoice_numbers:
            invoice_rows = conn.execute(
                f"""
                SELECT
                    dr.id,
                    dr.root_key AS invoice_number,
                    dr.root_date AS invoice_date,
                    dr.name,
                    dr.customer_code,
                    dr.assembled_page_count
                FROM document_roots dr
                WHERE dr.doc_type = 'invoices'
                  AND dr.root_key IN ({_placeholders(len(invoice_numbers))})
                """,
                    invoice_numbers,
                ).fetchall()
        invoices_by_number = {row["invoice_number"]: dict(row) for row in invoice_rows}

        credit_memo_rows_by_invoice = {}
        if invoice_numbers:
            credit_memo_rows = conn.execute(
                f"""
                SELECT
                    dr.id,
                    dr.root_key AS credit_memo_number,
                    dr.root_date AS credit_memo_date,
                    dr.name,
                    dr.customer_code,
                    dr.assembled_page_count,
                    rr.normalized_value AS invoice_number
                FROM document_refs rr
                JOIN document_roots dr ON dr.id = rr.document_root_id
                WHERE dr.doc_type = 'credit-memos'
                  AND rr.ref_type = 'invoice_no'
                  AND rr.normalized_value IN ({_placeholders(len(invoice_numbers))})
                ORDER BY dr.root_date, dr.root_key
                """,
                invoice_numbers,
            ).fetchall()
            for row in credit_memo_rows:
                credit_memo_rows_by_invoice.setdefault(row["invoice_number"], []).append(dict(row))

    for link in invoice_links:
        root_id = link["root_id"]
        root_payload = root_payloads.get(root_id)
        if not root_payload:
            continue
        invoice_number = link["invoice_number"]
        invoice_row = invoices_by_number.get(invoice_number)
        if not invoice_row:
            root_payload["unresolved_invoice_refs"].append({
                "source_doc_type": "receive-vouchers",
                "source_key": root_payload["voucher_number"],
                "bridge_type": "invoice_no",
                "ref_value": link["invoice_ref_value"],
                "normalized_value": invoice_number,
            })
            continue

        invoice_payload = _normalize_chain_node(
            node_id=invoice_row["id"],
            doc_type="invoices",
            key_value=invoice_row["invoice_number"],
            date_field=invoice_row.get("invoice_date"),
            name=invoice_row.get("name"),
            customer_code=invoice_row.get("customer_code"),
            assembled_page_count=invoice_row.get("assembled_page_count"),
        )
        invoice_payload["bridge_value"] = link["invoice_ref_value"]
        invoice_payload["credit_memos"] = [
            _normalize_chain_node(
                node_id=credit_memo["id"],
                doc_type="credit-memos",
                key_value=credit_memo["credit_memo_number"],
                date_field=credit_memo.get("credit_memo_date"),
                name=credit_memo.get("name"),
                customer_code=credit_memo.get("customer_code"),
                assembled_page_count=credit_memo.get("assembled_page_count"),
            )
            for credit_memo in credit_memo_rows_by_invoice.get(invoice_number, [])
            ]
        bridge_entry = _normalize_bridge_entry(link, invoice_payload)
        root_payload["invoice_bridges"].append(bridge_entry)
        root_payload["invoices"].append(invoice_payload)
        root_payload["chain_page_count"] += int(invoice_row.get("assembled_page_count") or 0)
        root_payload["chain_page_count"] += sum(
            int(credit_memo.get("assembled_page_count") or 0)
            for credit_memo in credit_memo_rows_by_invoice.get(invoice_number, [])
        )

    for root_payload in root_payloads.values():
        root_payload["chain_page_count"] += len(_confirmed_uob_page_refs(root_payload))
        root_payload["chain_page_count"] += len(_confirmed_kbank_page_refs(root_payload))
        mapping_summary = _build_mapping_summary(root_payload)
        root_payload["mapping_summary"] = mapping_summary
        root_payload["mapping_status"] = "complete" if mapping_summary["overall_complete"] else "incomplete"

    return [root_payloads[root_id] for root_id in root_ids if root_id in root_payloads]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    static_version = get_static_version()
    return templates.TemplateResponse(request, "index.html", {
        "static_version": static_version,
        "doc_types": DOC_TYPES,
    })


@app.get("/api/workspace/chain")
def workspace_chain(
    voucher_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
    include_json_view: bool = False,
):
    chains = _load_workspace_chain(
        voucher_number=voucher_number,
        date_from=date_from,
        date_to=date_to,
        ref_type=ref_type,
        ref_value=ref_value,
    )
    if include_json_view:
        return {
            "chains": chains,
            "json_view": _build_chain_json_view(chains),
        }
    return chains


@app.get("/api/workspace/quick-access/{doc_type}")
def workspace_quick_access(doc_type: str, q: str, limit: int = 30):
    if doc_type not in DOC_TYPE_INDEX:
        raise HTTPException(status_code=404, detail="Document type not found")
    return _load_quick_access_rows(doc_type, q, limit=limit)


def _workspace_chain_pdf_response(voucher_number: str) -> Response:
    chains = _load_workspace_chain(voucher_number=voucher_number)
    if not chains:
        raise HTTPException(status_code=404, detail="Voucher Number not found")

    pdf_parts: list[bytes] = []
    with db_session() as conn:
        for root in chains:
            pdf_parts.append(_read_chain_pdf(conn, "receive-vouchers", root["id"]))
            for bridge in root.get("invoice_bridges", []):
                if not bridge.get("resolved"):
                    continue
                invoice = bridge["invoice"]
                pdf_parts.append(_read_chain_pdf(conn, "invoices", invoice["id"]))
                for memo in invoice.get("credit_memos", []):
                    pdf_parts.append(_read_chain_pdf(conn, "credit-memos", memo["id"]))
            for page in _confirmed_kbank_page_refs(root):
                statement_reference = page.get("statement_reference")
                page_order = page.get("page_order")
                if not statement_reference or not page_order:
                    continue
                page_pdf = _read_statement_page_pdf(
                    conn,
                    "kbank-statements",
                    str(statement_reference),
                    int(page_order),
                )
                row_order = page.get("row_order")
                pdf_parts.append(
                    highlight_pdf_row(page_pdf, int(row_order), page.get("highlight_terms") or [])
                    if row_order
                    else page_pdf
                )
            for page in _confirmed_uob_page_refs(root):
                page_pdf = _read_statement_page_pdf(
                    conn,
                    "uob-ca-statements",
                    str(page["statement_key"]),
                    int(page["page_order"]),
                )
                pdf_parts.append(
                    highlight_pdf_row(
                        page_pdf,
                        int(page.get("row_order") or 1),
                        page.get("highlight_terms") or [],
                    )
                )

    output_pdf = assemble_pdf(pdf_parts)
    headers = {"Content-Disposition": f'inline; filename="{voucher_number}-chain.pdf"'}
    return Response(content=output_pdf, media_type="application/pdf", headers=headers)


@app.get("/api/receive-vouchers/{voucher_number}-chain.pdf")
def receive_voucher_chain_pdf(voucher_number: str):
    return _workspace_chain_pdf_response(voucher_number)


@app.get("/api/receive-vouchers/{voucher_number}.pdf")
def receive_voucher_pdf(voucher_number: str):
    return get_receive_voucher_pdf(voucher_number)


@app.get("/api/workspace/chain/{voucher_number}/pdf")
def workspace_chain_pdf(voucher_number: str):
    return _workspace_chain_pdf_response(voucher_number)


@app.get("/receive-vouchers", response_class=HTMLResponse)
def receive_vouchers_page(request: Request):
    static_version = get_static_version()
    return templates.TemplateResponse(request, "receive_vouchers.html", {"static_version": static_version})


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    static_version = get_static_version()
    return templates.TemplateResponse(request, "invoices.html", {"static_version": static_version})


@app.get("/credit-memos", response_class=HTMLResponse)
def credit_memos_page(request: Request):
    static_version = get_static_version()
    return templates.TemplateResponse(request, "credit_memos.html", {"static_version": static_version})


@app.get("/kbank-statements", response_class=HTMLResponse)
def kbank_statements_page(request: Request):
    static_version = get_static_version()
    search_fields = [
        {"id": "statementReference", "param": "statement_reference", "label": "Statement Reference", "placeholder": "ค้นหา Statement Reference… เช่น KB2025-001"},
        {"id": "accountNumber", "param": "account_number", "label": "Account Number"},
    ]
    columns = [
        {"key": "page_key", "label": "Page Key"},
        {"key": "statement_reference", "label": "Statement Reference"},
        {"key": "page_order", "label": "Page No."},
        {"key": "page_count", "label": "Pages"},
        {"key": "refs", "label": "Refs"},
    ]
    return templates.TemplateResponse(
        request,
        "bank_statements.html",
        {
            "static_version": static_version,
            "title": "KBank Statements",
            "search_fields": search_fields,
            "extra_filters": [],
            "ref_types": [
                ("transaction_ref", "Transaction Ref"),
                ("check_no", "Check No."),
            ],
            "columns": columns,
            "js_config": {
                "api": "/api/kbank-statements",
                "keyColumn": "page_key",
                "searchFields": search_fields,
                "extraFilters": [],
                "columns": columns,
                "pageResults": True,
                "pagesApiSuffix": "/pages",
                "pagePdfBase": "/api/kbank-statements/pages",
            },
        },
    )


@app.get("/uob-ca-statements", response_class=HTMLResponse)
def uob_ca_statements_page(request: Request):
    static_version = get_static_version()
    search_fields = [
        {"id": "statementKey", "param": "statement_key", "label": "Statement Key", "placeholder": "ค้นหา Statement Key… เช่น UOB-CA-XXXXXXXXXX-XXXXXXXX-XXXXXXXX"},
        {"id": "accountNumber", "param": "account_number", "label": "Account Number"},
        {"id": "companyId", "param": "company_id", "label": "Company ID"},
    ]
    extra_filters = [
        {"id": "statementDate", "param": "statement_date", "label": "Statement Date", "type": "date"},
    ]
    columns = [
        {"key": "page_key", "label": "Page Key"},
        {"key": "statement_key", "label": "Statement Key"},
        {"key": "page_order", "label": "Page No."},
        {"key": "page_count", "label": "Pages"},
        {"key": "refs", "label": "Refs"},
    ]
    return templates.TemplateResponse(
        request,
        "bank_statements.html",
        {
            "static_version": static_version,
            "title": "UOB Current Account Statements",
            "search_fields": search_fields,
            "extra_filters": extra_filters,
            "ref_types": [
                ("transaction_id", "Transaction ID"),
                ("transaction_ref", "Transaction Ref"),
                ("customer_ref", "Customer Ref"),
                ("customer_code", "Customer Code"),
                ("transaction_type", "Transaction Type"),
            ],
            "columns": columns,
            "js_config": {
                "api": "/api/uob-ca-statements",
                "keyColumn": "page_key",
                "searchFields": search_fields,
                "extraFilters": extra_filters,
                "columns": columns,
                "pageResults": True,
                "pagesApiSuffix": "/pages",
                "pagePdfBase": "/api/uob-ca-statements/pages",
            },
        },
    )
