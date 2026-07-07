from app.routers.statement_router import create_statement_router
from app.services.uob_ca_statement_extractor import normalize_ref
from app.services.uob_ca_statement_importer import import_uob_ca_statement_pdf


router = create_statement_router(
    prefix="/api/uob-ca-statements",
    tag="uob-ca-statements",
    table="uob_ca_statements",
    refs_table="uob_ca_statement_refs",
    foreign_key="uob_ca_statement_id",
    key_column="statement_key",
    jobs_table="upload_jobs",
    job_keys_column="statement_keys",
    doc_type="uob-ca-statements",
    allowed_ref_types={
        "account_number", "company_id", "bank_code", "transaction_id",
        "transaction_ref", "customer_ref", "customer_code", "transaction_type",
    },
    importer=import_uob_ca_statement_pdf,
    normalizer=normalize_ref,
    select_columns=[
        "id", "statement_key", "statement_date", "period_from", "period_to",
        "company_id", "account_number", "account_name", "account_type",
        "account_currency", "account_branch", "assembled_page_count", "updated_at",
    ],
    search_columns={"statement_key", "account_number", "company_id"},
    export_filename="uob_ca_statements_export.pdf",
)


@router.get("")
def search_uob_ca_statements(
    statement_key: str | None = None,
    account_number: str | None = None,
    company_id: str | None = None,
    statement_date: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    rows = router.state.search_impl(
        {
            "statement_key": statement_key,
            "account_number": account_number,
            "company_id": company_id,
        },
        period_from, period_to, ref_type, ref_value,
    )
    if statement_date:
        from app.routers.statement_router import normalize_date_filter
        target = normalize_date_filter(statement_date)
        rows = [row for row in rows if row["statement_date"] == target]
    return rows


@router.get("/export/by-filter/pdf")
def export_uob_ca_statements(
    statement_key: str | None = None,
    account_number: str | None = None,
    company_id: str | None = None,
    statement_date: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    rows = search_uob_ca_statements(
        statement_key, account_number, company_id, statement_date,
        period_from, period_to, ref_type, ref_value,
    )
    return router.state.export_impl(rows)
