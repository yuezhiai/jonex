"""治理 P1：策略判定（纯函数，参考 Ranger 的 deny>allow + 掩码/行过滤语义）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch

from .levels import level_rank


@dataclass(frozen=True)
class Decision:
    effect: str                         # allow|deny|mask
    policy_id: str | None = None
    reason: str = ""
    mask_type: str | None = None
    row_filters: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active(policy: dict) -> bool:
    if not policy.get("enabled", True):
        return False
    now = _now()
    for key, op in (("valid_from", "ge"), ("valid_to", "le")):
        raw = policy.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if op == "ge" and now < dt:
            return False
        if op == "le" and now > dt:
            return False
    return True


def _match(policy: dict, *, table: str, column: str | None,
           tags: set[str]) -> bool:
    rtype = str(policy.get("resource_type") or "table")
    if rtype == "tag":
        return str(policy.get("tag_name") or "") in tags
    pattern = str(policy.get("table_pattern") or "")
    if not pattern or not (fnmatch(table, pattern)
                           or fnmatch(table.split(".")[-1], pattern)):
        return False
    if rtype == "column":
        cpat = str(policy.get("column_pattern") or "")
        if not column or not cpat:
            return False
        return fnmatch(column, cpat)
    return True


def decide(*, clearance: str, level: str | None, tags: list[str],
           policies: list[dict], action: str,
           table: str = "", column: str | None = None) -> Decision:
    """判定单个引用。action 只影响显式策略的匹配（当前策略模型不分动作）。"""
    tag_set = {str(t) for t in (tags or [])}
    matched = [p for p in (policies or [])
               if _active(p) and _match(p, table=table, column=column, tags=tag_set)]

    def applies(p: dict) -> bool:
        return level_rank(clearance) < level_rank(p.get("min_clearance") or "public")

    denies = sorted([p for p in matched if p.get("effect") == "deny" and applies(p)],
                    key=lambda p: (-int(p.get("priority") or 0), str(p.get("id"))))
    if denies:
        return Decision("deny", str(denies[0]["id"]),
                        f"策略 {denies[0].get('name') or denies[0]['id']} 拒绝")
    masks = sorted([p for p in matched if p.get("effect") == "mask" and applies(p)],
                   key=lambda p: (-int(p.get("priority") or 0), str(p.get("id"))))
    allows = sorted([p for p in matched if p.get("effect") == "allow" and not applies(p)],
                    key=lambda p: (-int(p.get("priority") or 0), str(p.get("id"))))
    filters = tuple(str(p.get("row_filter")) for p in matched
                    if p.get("effect") == "row_filter" and applies(p)
                    and p.get("row_filter"))
    if masks:
        return Decision("mask", str(masks[0]["id"]),
                        f"策略 {masks[0].get('name') or masks[0]['id']} 掩码",
                        str(masks[0].get("mask_type") or "null"), filters)
    if allows:
        return Decision("allow", str(allows[0]["id"]), "策略允许", None, filters)
    if filters:
        return Decision("allow", None, "仅行过滤", None, filters)
    # 默认档
    if level is None:
        return Decision("allow")
    if level_rank(clearance) >= level_rank(level):
        return Decision("allow")
    if str(level) == "confidential":
        return Decision("mask", None, "默认：机密级列掩码", "null")
    return Decision("deny", None, f"默认：{level} 级需要更高密级")
