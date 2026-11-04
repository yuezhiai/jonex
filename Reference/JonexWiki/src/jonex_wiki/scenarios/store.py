"""Per-KB state, optimistic revisions, immutable outputs, bounded diagnostic events."""

from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from jonex_wiki.kernel.errors import ValidationError


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def scope_root(layout, tenant, kb):
    if not all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", v or "") for v in (tenant, kb)
    ):
        raise ValidationError("SCENARIO_SCOPE_INVALID")
    root = layout.kb_dir(tenant, kb).resolve()
    if not root.is_relative_to(layout.root.resolve()):
        raise ValidationError("SCENARIO_SCOPE_INVALID")
    return root / "scenarios"


def read_state(layout, tenant, kb):
    path = scope_root(layout, tenant, kb) / "state.json"
    return (
        json.loads(path.read_text())
        if path.is_file()
        else {"schema": "wiki.scenarios.v1", "version": 0, "scenarios": {}}
    )


def write_state(layout, tenant, kb, state):
    state["updated_at"] = now()
    layout.atomic_write(
        scope_root(layout, tenant, kb) / "state.json",
        json.dumps(state, ensure_ascii=False, indent=2).encode(),
    )


def write_output(layout, tenant, kb, value):
    sha = digest(value)
    path = scope_root(layout, tenant, kb) / "versions" / (sha + ".json")
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if not path.exists():
        layout.atomic_write(path, data)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValidationError("SCENARIO_OUTPUT_HASH_MISMATCH")
    return sha


def read_output(layout, tenant, kb, sha):
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise ValidationError("SCENARIO_REVISION_INVALID")
    path = scope_root(layout, tenant, kb) / "versions" / (sha + ".json")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValidationError("SCENARIO_OUTPUT_HASH_MISMATCH")
    return json.loads(raw)


def event(layout, tenant, kb, job, name, **details):
    if not re.fullmatch(r"[0-9a-f]{32}", job):
        raise ValidationError("SCENARIO_JOB_INVALID")
    path = scope_root(layout, tenant, kb) / "jobs" / (job + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(
            json.dumps({"at": now(), "event": name, **details}, ensure_ascii=False)
            + "\n"
        )
