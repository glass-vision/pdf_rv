from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import db_session
from app.services.reconciliation import (
    confirm_rv_kbank_candidate,
    confirm_rv_uob_candidate,
    confirm_unique_rv_kbank_candidates,
    confirm_unique_rv_uob_candidates,
    find_rv_kbank_candidates,
    find_rv_uob_candidates,
    unconfirm_rv_kbank_candidate,
    unconfirm_rv_uob_candidate,
)


router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


class ConfirmRvUobCandidateRequest(BaseModel):
    bank_row_id: str
    statement_key: str
    page_order: int
    row_order: int
    match_rule: str | None = None
    match_conditions: list[str] | None = None
    selection_source: str = "manual"


class ConfirmRvKbankCandidateRequest(BaseModel):
    bank_row_id: str
    statement_key: str
    page_order: int
    row_order: int
    match_rule: str | None = None
    match_conditions: list[str] | None = None
    selection_source: str = "manual"


@router.get("/rv-uob/candidates")
def get_rv_uob_candidates(voucher_number: str | None = None):
    with db_session() as conn:
        return find_rv_uob_candidates(conn, voucher_number=voucher_number)


@router.post("/rv-uob/confirm")
def post_confirm_rv_uob_candidate(payload: ConfirmRvUobCandidateRequest):
    with db_session() as conn:
        try:
            return confirm_rv_uob_candidate(
                conn,
                bank_row_id=payload.bank_row_id,
                statement_key=payload.statement_key,
                page_order=payload.page_order,
                row_order=payload.row_order,
                match_rule=payload.match_rule,
                match_conditions=payload.match_conditions,
                selection_source=payload.selection_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rv-uob/confirm-unique")
def post_confirm_unique_rv_uob_candidates(voucher_number: str):
    with db_session() as conn:
        return confirm_unique_rv_uob_candidates(conn, voucher_number=voucher_number)


@router.delete("/rv-uob/confirm/{bank_row_id}")
def delete_confirmed_rv_uob_candidate(bank_row_id: str):
    with db_session() as conn:
        return unconfirm_rv_uob_candidate(conn, bank_row_id=bank_row_id)


@router.get("/rv-kbank/candidates")
def get_rv_kbank_candidates(voucher_number: str | None = None):
    with db_session() as conn:
        return find_rv_kbank_candidates(conn, voucher_number=voucher_number)


@router.post("/rv-kbank/confirm")
def post_confirm_rv_kbank_candidate(payload: ConfirmRvKbankCandidateRequest):
    with db_session() as conn:
        try:
            return confirm_rv_kbank_candidate(
                conn,
                bank_row_id=payload.bank_row_id,
                statement_key=payload.statement_key,
                page_order=payload.page_order,
                row_order=payload.row_order,
                match_rule=payload.match_rule,
                match_conditions=payload.match_conditions,
                selection_source=payload.selection_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rv-kbank/confirm-unique")
def post_confirm_unique_rv_kbank_candidates(voucher_number: str):
    with db_session() as conn:
        return confirm_unique_rv_kbank_candidates(conn, voucher_number=voucher_number)


@router.delete("/rv-kbank/confirm/{bank_row_id}")
def delete_confirmed_rv_kbank_candidate(bank_row_id: str):
    with db_session() as conn:
        return unconfirm_rv_kbank_candidate(conn, bank_row_id=bank_row_id)
