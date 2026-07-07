from app.routers.statement_router import create_statement_router
from app.services.kbank_statement_extractor import extract_kbank_statement, normalize_ref
from app.services.kbank_statement_importer import import_kbank_statement_pdf


router = create_statement_router(
    prefix="/api/kbank-statements",
    tag="kbank-statements",
    table="kbank_statements",
    refs_table="kbank_statement_refs",
    foreign_key="kbank_statement_id",
    key_column="statement_reference",
    jobs_table="upload_jobs",
    job_keys_column="statement_references",
    doc_type="kbank-statements",
    allowed_ref_types={
        "account_number", "statement_reference", "bank_code",
        "transaction_ref", "check_no",
    },
    importer=import_kbank_statement_pdf,
    normalizer=normalize_ref,
    select_columns=[
        "id", "statement_reference", "period_from", "period_to", "account_number",
        "account_name", "branch_name", "assembled_page_count", "updated_at",
    ],
    search_columns={"statement_reference", "account_number"},
    export_filename="kbank_statements_export.pdf",
    page_extractor=extract_kbank_statement,
)


@router.get("")
def search_kbank_statements(
    statement_reference: str | None = None,
    account_number: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    return router.state.search_impl(
        {"statement_reference": statement_reference, "account_number": account_number},
        period_from, period_to, ref_type, ref_value,
    )


@router.get("/export/by-filter/pdf")
def export_kbank_statements(
    statement_reference: str | None = None,
    account_number: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    rows = search_kbank_statements(
        statement_reference, account_number, period_from, period_to, ref_type, ref_value
    )
    return router.state.export_impl(rows)
