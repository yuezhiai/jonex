"""Tenant-scoped, short-lived Wiki search sessions shared across workers."""
import re

SESSION_TTL = 1200


def session_key(tenant_id: str, session_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id or ""):
        raise ValueError("Invalid Wiki search session")
    return f"wiki-search:{tenant_id}:{session_id}"
