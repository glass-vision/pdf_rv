from __future__ import annotations

from fastapi import APIRouter

from app.database import db_session
from app.services.reconciliation import find_rv_uob_candidates


router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.get("/rv-uob/candidates")
def get_rv_uob_candidates(voucher_number: str | None = None):
    with db_session() as conn:
        return find_rv_uob_candidates(conn, voucher_number=voucher_number)
