from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from sqlite3 import Connection
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def delete_document_core(conn: Connection, doc_type: str, root_key: str) -> None:
    row = conn.execute(
        "SELECT id FROM document_roots WHERE doc_type = ? AND root_key = ?",
        (doc_type, root_key),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM document_roots WHERE id = ?", (row["id"],))


def fetch_document_root(conn: Connection, doc_type: str, root_key: str):
    return conn.execute(
        """
        SELECT
            id,
            doc_type,
            root_key,
            display_key,
            root_date,
            period_from,
            name,
            customer_code,
            assembled_storage_mode,
            assembled_pdf,
            assembled_pdf_path,
            assembled_page_count,
            assembled_at,
            created_at,
            updated_at
        FROM document_roots
        WHERE doc_type = ? AND root_key = ?
        """,
        (doc_type, root_key),
    ).fetchone()


def fetch_document_refs(conn: Connection, root_id: str) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT ref_type, ref_value
        FROM document_refs
        WHERE document_root_id = ?
        ORDER BY created_at, ref_value
        """,
        (root_id,),
    ).fetchall()
    refs: dict[str, list[str]] = {}
    for row in rows:
        refs.setdefault(row["ref_type"], []).append(row["ref_value"])
    return refs


def fetch_document_pages_with_refs(conn: Connection, root_id: str) -> list[dict]:
    pages = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, page_order, raw_text
            FROM document_pages
            WHERE document_root_id = ?
            ORDER BY page_order
            """,
            (root_id,),
        ).fetchall()
    ]
    if not pages:
        return []

    page_ids = [page["id"] for page in pages]
    placeholders = ",".join("?" for _ in page_ids)
    ref_rows = conn.execute(
        f"""
        SELECT document_page_id, ref_type, ref_value
        FROM document_page_refs
        WHERE document_page_id IN ({placeholders})
        ORDER BY created_at, ref_value
        """,
        page_ids,
    ).fetchall()
    refs_by_page: dict[str, dict[str, list[str]]] = {}
    for row in ref_rows:
        page_refs = refs_by_page.setdefault(row["document_page_id"], {})
        page_refs.setdefault(row["ref_type"], []).append(row["ref_value"])

    for page in pages:
        page["refs"] = refs_by_page.get(page["id"], {})
    return pages


def fetch_document_page_by_order(conn: Connection, root_id: str, page_order: int):
    return conn.execute(
        """
        SELECT id, page_order, storage_mode, page_pdf, page_pdf_path, raw_text
        FROM document_pages
        WHERE document_root_id = ? AND page_order = ?
        """,
        (root_id, page_order),
    ).fetchone()


def fetch_document_pdf(conn: Connection, doc_type: str, root_key: str):
    return fetch_document_root(conn, doc_type, root_key)


def search_document_roots(
    conn: Connection,
    *,
    doc_type: str,
    root_key: str | None = None,
    root_date_from: str | None = None,
    root_date_to: str | None = None,
    period_from_max: str | None = None,
    customer_code: str | None = None,
    ref_type: str | None = None,
    ref_value: str | None = None,
):
    """Search document_roots. Returns generic column names; callers rename as needed."""
    sql = """
        SELECT DISTINCT
            dr.id,
            dr.root_key,
            dr.root_date,
            dr.period_from,
            dr.name,
            dr.customer_code,
            dr.assembled_page_count,
            dr.updated_at
        FROM document_roots dr
    """
    params: list[str] = [doc_type]
    where = ["dr.doc_type = ?"]

    if ref_type and ref_value:
        sql += " JOIN document_refs rr ON rr.document_root_id = dr.id"
        where.append("rr.ref_type = ?")
        params.append(ref_type)
        if doc_type == "receive-vouchers" and ref_type == "check_no":
            where.append(
                "("
                "rr.normalized_value = ? "
                "OR rr.normalized_value LIKE ? "
                "OR rr.normalized_value GLOB ? "
                "OR rr.normalized_value GLOB ?"
                ")"
            )
            params.extend([
                ref_value,
                f"{ref_value}#%",
                f"{ref_value}-[0-9]*#*",
                f"{ref_value}-[0-9]*",
            ])
        else:
            where.append("rr.normalized_value = ?")
            params.append(ref_value)

    if root_key:
        where.append("dr.root_key LIKE ?")
        params.append(f"%{root_key}%")

    if root_date_from:
        where.append("dr.root_date IS NOT NULL AND dr.root_date >= ?")
        params.append(root_date_from)

    if root_date_to:
        where.append("dr.root_date IS NOT NULL AND dr.root_date <= ?")
        params.append(root_date_to)

    if period_from_max:
        where.append("dr.period_from IS NOT NULL AND dr.period_from <= ?")
        params.append(period_from_max)

    if customer_code:
        where.append("dr.customer_code LIKE ?")
        params.append(f"%{customer_code}%")

    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY dr.root_date, dr.root_key"
    return conn.execute(sql, params).fetchall()


def insert_document_core(
    conn: Connection,
    *,
    doc_type: str,
    root_key: str,
    display_key: str,
    root_date: str | None,
    period_from: str | None = None,
    name: str | None,
    customer_code: str | None,
    storage_mode: str,
    assembled_pdf: bytes | None,
    assembled_pdf_path: str | None,
    assembled_page_count: int,
    page_rows: Iterable[tuple[int, bytes | None, str | None, str | None]],
    refs: dict[str, set[str]],
    page_ref_rows: Iterable[tuple[int, dict[str, set[str]]]] | None = None,
) -> str:
    now = utc_now()
    root_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO document_roots (
            id, doc_type, root_key, display_key, root_date, period_from,
            name, customer_code, assembled_storage_mode,
            assembled_pdf, assembled_pdf_path, assembled_page_count,
            assembled_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            root_id,
            doc_type,
            root_key,
            display_key,
            root_date,
            period_from,
            name,
            customer_code,
            storage_mode,
            assembled_pdf,
            assembled_pdf_path,
            assembled_page_count,
            now,
            now,
            now,
        ),
    )

    page_ids_by_order: dict[int, str] = {}
    for page_order, page_pdf, page_pdf_path, raw_text in page_rows:
        page_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO document_pages (
                id, document_root_id, page_order, storage_mode, page_pdf,
                page_pdf_path, raw_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                root_id,
                page_order,
                storage_mode,
                page_pdf,
                page_pdf_path,
                raw_text,
                now,
            ),
        )
        page_ids_by_order[page_order] = page_id

    if page_ref_rows:
        for page_order, page_refs in page_ref_rows:
            page_id = page_ids_by_order.get(page_order)
            if not page_id:
                continue
            for ref_type, values in page_refs.items():
                for value in sorted(values):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO document_page_refs (
                            id, document_page_id, ref_type, ref_value, normalized_value, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), page_id, ref_type, value, value, now),
                    )

    for ref_type, values in refs.items():
        for value in sorted(values):
            conn.execute(
                """
                INSERT OR IGNORE INTO document_refs (
                    id, document_root_id, ref_type, ref_value, normalized_value, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), root_id, ref_type, value, value, now),
            )

    return root_id
