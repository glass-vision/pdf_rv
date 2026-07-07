from __future__ import annotations


def normalize_client_id(client_id: str | None) -> str:
    value = (client_id or "").strip()
    if not value:
        return ""
    return value[:128]
