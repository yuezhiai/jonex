# -*- coding:utf-8 -*-
"""演化台 `metric` kind 的纯函数 apply / 回读核对。

`metric` 属 abstract 家族（与 alias / mapping / tool_schema 同链、同版本、
同回滚），存储是抽象 schema 的 `schema.metrics`。本模块只在内存里合并：

- 同名已存在 → update（记录 `before`，供采纳台账回滚）；
- 不存在且 `op=add`（或缺省）→ 新建；
- `disable` → `enabled=false`；`enable` → 删键；
- 新名受 `MAX_METRICS` 约束，超限 skipped（更新既有指标仍放行）。

条目的 `definition` 落到指标的 `description`（`auto_hints` 读
`definition` 或 `description`），`formula` 原样存（读侧同样认它）。
落库归一（`concept_expansion._normalize_metric`）会保留 `formula` 与显式
`enabled=false`，否则 disable 写完即丢、回读核对恒失败。

不碰 alias / mapping / tool_schema 的任何字段。
"""
from __future__ import annotations

import copy

from ..concept_expansion import (
    MAX_ALIASES_PER_CONCEPT,
    MAX_LANDINGS_PER_METRIC,
    MAX_METRICS,
    MAX_TERM_LENGTH,
)

#: 允许的 op（缺省按「同名 update / 异名 add」推断）。
METRIC_OPS = frozenset({"add", "update", "disable", "enable"})


def _text(value) -> str:
    """标量 → 去空白字符串；容器一律当空（与 concept_expansion 同口径）。"""
    if value is None or isinstance(
            value, (list, tuple, dict, set, frozenset, bytes, bytearray)):
        return ""
    return str(value).strip()


def _as_list(values) -> list:
    if isinstance(values, (list, tuple, set, frozenset)):
        return list(values)
    if values is None or isinstance(values, (dict, bytes, bytearray)):
        return []
    if isinstance(values, str):
        return [values]
    if hasattr(values, "__iter__"):
        return list(values)
    return []


def _aliases(values) -> list[str]:
    """别名归一：去空白、丢空/超长、去重保序，条数受 `MAX_ALIASES_PER_CONCEPT`。"""
    out: list[str] = []
    seen: set[str] = set()
    for value in _as_list(values):
        alias = _text(value)
        if not alias or len(alias) > MAX_TERM_LENGTH or alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
        if len(out) >= MAX_ALIASES_PER_CONCEPT:
            break
    return out


def _landings(values) -> list[dict]:
    """落点归一（与 `_unique_metric_landings` 同口径）。

    `data_source_id` 必填；`di_metric` / `entity_name` 至少其一非空；三键去重
    （先到者胜）；受 `MAX_LANDINGS_PER_METRIC` 约束；空通道**省键**。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for value in _as_list(values):
        if not isinstance(value, dict):
            continue
        ds = _text(value.get("data_source_id"))
        di_metric = _text(value.get("di_metric"))
        entity = _text(value.get("entity_name"))
        if not ds or not (di_metric or entity):
            continue
        key = (ds, di_metric, entity)
        if key in seen:
            continue
        seen.add(key)
        item: dict = {"data_source_id": ds}
        if di_metric:
            item["di_metric"] = di_metric
        if entity:
            item["entity_name"] = entity
        out.append(item)
        if len(out) >= MAX_LANDINGS_PER_METRIC:
            break
    return out


def _apply_fields(metric: dict, entry: dict) -> bool:
    """把条目的可选槽写进 metric；返回 landings 是否非法（调用方据此 skip）。"""
    if "aliases" in entry:
        metric["aliases"] = _aliases(entry.get("aliases"))
    # `definition` 与 `description` 同义：落库槽叫 description（definition 优先，
    # 为空时回退 description；两者都空 = 明确清除旧描述）。
    if "definition" in entry or "description" in entry:
        text = _text(entry.get("definition")) or _text(entry.get("description"))
        if text:
            metric["description"] = text
        else:
            metric.pop("description", None)
    if "formula" in entry:
        text = _text(entry.get("formula"))
        if text:
            metric["formula"] = text
        else:
            metric.pop("formula", None)
    if "display_name" in entry:
        text = _text(entry.get("display_name"))
        if text:
            metric["display_name"] = text
        else:
            metric.pop("display_name", None)
    if "landings" in entry:
        raw = _as_list(entry.get("landings"))
        normalized = _landings(raw)
        # 提供了落点却一条都不合法 = 形状错误：整条 skip，绝不静默清空旧落点。
        if raw and not normalized:
            return False
        metric["landings"] = normalized
    metric.setdefault("aliases", [])
    metric.setdefault("landings", [])
    return True


def apply_metric_entries(schema: dict,
                         entries: list[dict]) -> tuple[dict, list, list]:
    """抽象 schema → (新 schema, added, skipped)：metric 的 add/update/disable/enable。

    `added` 记 `{op, name, before, after}`：`before` 是改动前的整条指标
    （新建为 None，供回滚删除）；`after` 是应用后的整条指标（回读核对用）。
    """
    working = copy.deepcopy(schema or {})
    metrics = [dict(m) for m in working.get("metrics") or []
               if isinstance(m, dict)]
    working["metrics"] = metrics
    index = {str(m.get("name") or ""): i for i, m in enumerate(metrics)}

    added: list[dict] = []
    skipped: list[dict] = []

    for entry in entries or []:
        if not isinstance(entry, dict):
            skipped.append({"why": "invalid_entry"})
            continue
        name = _text(entry.get("name"))
        wanted_op = _text(entry.get("op")).lower()
        if not name:
            skipped.append({"name": name, "op": wanted_op or None,
                            "why": "empty_name"})
            continue
        if len(name) > MAX_TERM_LENGTH:
            skipped.append({"name": name, "op": wanted_op or None,
                            "why": "name_too_long"})
            continue
        pos = index.get(name)
        op = wanted_op or ("update" if pos is not None else "add")
        if op not in METRIC_OPS:
            skipped.append({"name": name, "op": op, "why": "unknown_op"})
            continue
        if op == "add" and pos is not None:
            # 同名存在 → update（「add 已存在同名」不报错、不重复建）。
            op = "update"
        if op in ("update", "disable", "enable") and pos is None:
            skipped.append({"name": name, "op": op, "why": "not_found"})
            continue
        if op == "add" and len(metrics) >= MAX_METRICS:
            skipped.append({"op": "add", "name": name, "why": "max_metrics"})
            continue

        if pos is None:
            before = None
            metric: dict = {"name": name, "aliases": [], "landings": []}
        else:
            before = copy.deepcopy(metrics[pos])
            metric = copy.deepcopy(metrics[pos])

        if op in ("add", "update"):
            if not _apply_fields(metric, entry):
                skipped.append({"name": name, "op": op,
                                "why": "invalid_landings"})
                continue
        elif op == "disable":
            metric["enabled"] = False
        else:  # enable
            metric.pop("enabled", None)

        if pos is None:
            index[name] = len(metrics)
            metrics.append(metric)
        else:
            metrics[pos] = metric
        added.append({"op": op, "name": name, "before": before,
                      "after": copy.deepcopy(metric)})

    return working, added, skipped


def _metric_matches(metric: dict, record: dict) -> bool:
    """单条 added 记录的目标状态是否已在落库指标里（只看本次写入的槽）。"""
    op = str(record.get("op") or "")
    if op == "disable":
        return metric.get("enabled") is False
    if op == "enable":
        return metric.get("enabled") is not False
    after = record.get("after")
    if not isinstance(after, dict):
        return False
    if str(metric.get("name")) != str(after.get("name")):
        return False
    for key, value in after.items():
        if key == "enabled":
            if metric.get("enabled") != value:
                return False
            continue
        if key == "name":
            continue
        if metric.get(key) != value:
            return False
    return True


def metric_added_verified(schema: dict, added: list[dict]) -> bool:
    """回读核对：`added` 记录的**终态**是否已在 schema.metrics 里（纯函数）。

    同一 name 在一批里被多次操作时只看**最后一条**（写库后是整批终态，拿
    中间态逐条比会把合法批次误报成「回读核对失败」）。
    - add / update：目标指标存在，且记录里写过的槽逐个一致；
    - disable：`enabled` 必须恰好是 `False`；
    - enable：`enabled` 不能是 `False`（落库是删键）。
    """
    terminal: dict[str, dict] = {}
    for record in added or []:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "")
        if name:
            terminal[name] = record
    metrics = {str(m.get("name")): m
               for m in (schema or {}).get("metrics") or []
               if isinstance(m, dict)}
    for name, record in terminal.items():
        metric = metrics.get(name)
        if metric is None or not _metric_matches(metric, record):
            return False
    return True


def _restore_metric_diff(live: dict, before: dict, after: dict) -> dict:
    """只回退 `before` 与 `after` 之间**实际不同**的键。

    `before` 有而 `after` 无 → 重置为 before 值；`before` 无而 `after` 有 →
    删键；两处相同 → 本批没碰这个键，保留 `live` 现值（并发改过的其它键不被
    顺手回退）。返回新 dict，不改入参。
    """
    out = copy.deepcopy(live)
    for key, value in before.items():
        if key not in after or after[key] != value:
            out[key] = copy.deepcopy(value)
    for key in after:
        if key not in before:
            out.pop(key, None)
    return out


def rollback_metric_entries(schema: dict, added: list[dict]) -> dict:
    """逆操作本批 metric 改动（按 name 定位；找不到的忽略 = 幂等）。

    **逆序**处理，保证同名指标在一批里被多次改（update → disable）时逐项复原：

    - `add`（`before is None`）→ 删除该 name 的指标（本批新建的）。**只有当前
      内容仍是本批写入的 `after` 时才删**；`after` 缺失（旧补丁）退回按 name
      删除；期间被另一采纳改过 → 跳过，绝不整条删掉别人的改动。
    - `update` / `enable` → 只恢复 `before` 与 `after` 之间实际不同的键。`after`
      缺失（旧补丁）才退回用 `before` 整条覆盖；这样并发改过的其它键保持不动。
    - `disable` → 按 `before` 恢复 `enabled`（缺键 = 删键）。

    未见过的 name 跳过；返回与 `rollback_patch` 其它分支同形状的整份 schema，
    调用方据此做 `==` 逐键比较（内容没变就不写新版本）。没有任何实际回滚时
    原样返回入参的深拷贝——过滤非 dict 脏元素不算内容变化。
    """
    working = copy.deepcopy(schema or {})
    metrics = [dict(m) for m in working.get("metrics") or []
               if isinstance(m, dict)]
    changed = False

    for record in reversed(list(added or [])):
        if not isinstance(record, dict):
            continue
        name = _text(record.get("name"))
        if not name:
            continue
        op = _text(record.get("op")).lower()
        before = record.get("before")
        after = record.get("after")
        pos = next((i for i, metric in enumerate(metrics)
                    if str(metric.get("name") or "") == name), None)
        if op == "add" or (not op and before is None):
            if before is not None:
                # 形状不对（add 必是新建）：不删别人的条目。
                continue
            if pos is None:
                continue
            if after is not None and metrics[pos] != after:
                # 期间另一采纳更新了同名指标：旧补丁不得整条删掉它。
                continue
            metrics.pop(pos)
            changed = True
            continue
        if pos is None:
            # 条目早已不在（被并发删掉 / 回滚重放）→ 跳过，不留空版本。
            continue
        if op == "disable":
            if isinstance(before, dict) and "enabled" in before:
                metrics[pos]["enabled"] = before["enabled"]
            else:
                metrics[pos].pop("enabled", None)
            changed = True
            continue
        if op in ("update", "enable") and isinstance(before, dict):
            if isinstance(after, dict):
                metrics[pos] = _restore_metric_diff(
                    metrics[pos], before, after)
            else:
                metrics[pos] = copy.deepcopy(before)
            changed = True
            continue
        # 未知 op：`before` 完整时按整条恢复，否则跳过（与「找不到忽略」同姿态）。
        if isinstance(before, dict):
            metrics[pos] = copy.deepcopy(before)
            changed = True

    if not changed:
        # 没有实际回滚：原样返回（不因过滤脏元素而制造内容变化）。
        return working
    # 原本就没有 `metrics` 键时不要凭空造一个空数组（否则「条目早已不在」
    # 的旧补丁会被当成内容变化、留下一个空版本，与 tool_schema 同口径）。
    if metrics or "metrics" in working:
        working["metrics"] = metrics
    return working


__all__ = ["METRIC_OPS", "apply_metric_entries", "metric_added_verified",
           "rollback_metric_entries"]
