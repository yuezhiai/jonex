"""把审计里的 ② 失败归因到 C/T/S 并产出提案（P2-1）。**纯函数**。

三件事：过滤（窗口/租户）→ 归因（规则表）→ 聚合出提案（稳定 id、去重、reject 过滤）。
不连库、不调模型、**不写任何东西** —— 库读与文件写都在 CLI 那层。

归因规则照报告 §3.2 B5 给的映射：内容类（概念/别名缺失、覆盖重叠、列或口径缺）
归 C；执行面/Schema/登记类归 S；提示词/模型/传输归 T；**认不出的码单列 `unknown`、
不猜**（新加的码先补表）。
"""
from __future__ import annotations

import datetime
import hashlib
from collections import defaultdict

from ..concept_expansion import AGGREGATE_WORDS

#: reason_code → (归因档, 提案类型)。归因档：C 内容 / S Schema / T 工具 / unknown。
ATTRIBUTIONS: dict[str, tuple[str, str]] = {
    "no_candidate": ("C", "concept_gap"),
    "ambiguous": ("C", "concept_overlap"),
    "not_answerable": ("C", "columns_or_convention_gap"),
    "rejected_unsafe_sql": ("T", "prompt_or_guard"),
    # 治理拒绝（D1）：政策命中不是模型缺陷，归工具档但单列 kind；
    # 谓词/聚合里的「掩码位置」由 `attribute()` 按 scene.rejection_kind 再细化。
    "rejected_sensitive": ("T", "governance_policy"),
    "llm_failed": ("T", "model_or_transport"),
    "entity_gone": ("S", "datasource_registration"),
    "source_gone": ("S", "datasource_registration"),
    "not_a_database_source": ("S", "datasource_registration"),
    "no_readonly_creds": ("S", "datasource_registration"),
}

#: `exec_failed` 的两个子档：物理落点缺失（C1 型）/ 其它执行面。
#: ⚠️ 判据要**覆盖真源的方言**：唯一在册库源是 SQL Server，缺表报错是 `Invalid object name 'X'`
#: （Postgres 才是 `relation ... does not exist`）—— 所以比对**小写化后的文本**，marker 全小写。
MISSING_RELATION_MARKERS = ("does not exist", "不存在", "invalid object name", "对象名")

#: 单条提案最多带几个样本（question + sql/rejected_sql + reason）。
MAX_SAMPLES = 3

#: `exec_other` 的动作文案，同时是**未补表 kind 的兜底**：
#: 新增 kind 必须同时补 `_ACTIONS`，否则静默落到这里（给错方向）。
#: `exec_other` 自身也显式入表 —— 守护用例按「kind 全表穷尽」钉，缺一个键就红。
_EXEC_OTHER_ACTION = "执行面问题（权限/超时/方言）：按 reason 文本分档"

#: 归因档 → 建议动作（提案里给人看的一句话）。
_ACTIONS = {
    "concept_gap": "给这些问句涉及的概念补别名/补概念（先看样本定位是哪个概念）",
    "concept_overlap": "概念覆盖重叠：拆别名或压掉其中一个（看样本里它命中了谁）",
    "columns_or_convention_gap": "列不够或口径缺失：看样本判是概念缺失还是缺一条「口径」约束",
    "missing_relation": "物理落点缺失（C1 型）：限定名/表名对不上 → 重生成 schema 或修 source_object_id",
    "datasource_registration": "数据源登记/凭据问题：补只读账号或修连接",
    "prompt_or_guard": "提示词/guard：模型反复想写这类 SQL（看 rejected_sql 样本）",
    "forbidden_pair": "禁止同查命中：核对这对表是否确实不能同查；策略正确时改提示词/注入剔边",
    "governance_policy": "治理策略拒绝：核对用户密级/标签/策略是否符合预期（不是模型缺陷）",
    "governance_mask_position": "敏感列被用于过滤/聚合：提高用户密级或调整策略（掩码列不能进谓词）",
    "model_or_transport": "模型/传输抖动：看是否集中于某时段（重试可能有用）",
    "unknown_code": "认不出的 reason_code：先把它补进归因表，别猜",
    "exec_other": _EXEC_OTHER_ACTION,
}

#: `concept_gap` 且问句含聚合词时的细化文案（T6）——**不新增 attribution kind**
#: （proposal id = sha1(归因|kind|sub_key|ds)，kind 面一动旧 proposal 全换号；
#: 且守护测试断言 `kinds <= set(_ACTIONS)`——细化文案是唯一不破这两者的路）。
_METRIC_GAP_ACTION = (
    "问句疑似在问『指标』（含合计/数量词）而抽象 schema 未登记对应指标——"
    "把该指标补进指标字段（画布「指标」面板），或在本页采纳为指标别名"
)

#: `columns_or_convention_gap` 档的现场细化文案（2026-10-12 观测线）——与
#: `_METRIC_GAP_ACTION` 同口径：**不新增 attribution kind**（proposal id 含 kind、
#: 守护测试断言 `kinds <= set(_ACTIONS)`；细化文案是唯一不破这两者的路）。
#: `no_samples` 优先于 `tools_unused`（查过没样本 ≠ 没查）。
_NO_SAMPLES_ACTION = "模型查过样本但该列无样本——先给该列跑一次探测（或补口径），再判是否真缺列"
_TOOLS_UNUSED_ACTION = "模型零工具调用即放弃——先看引导/提示词（多轮档下它没去查）"


def _as_list(v) -> list:
    """脏 scene 防御：非 list（None / 标量 / dict）一律当空列表。"""
    return v if isinstance(v, list) else []


def _scene_hit(items, pred) -> bool:
    """组内任一条目的 scene 满足 pred 即真（无 scene / 非 dict 的条目跳过、不触发）。"""
    for it in items or []:
        scene = it.get("scene") if isinstance(it, dict) else None
        if isinstance(scene, dict) and pred(scene):
            return True
    return False


def _action_for(kind: str, items) -> str:
    """归因 → 行动文案。细化只在两处：`concept_gap` 的指标缺口（T6）、
    `columns_or_convention_gap` 的现场判据（2026-10-12 观测线）。"""
    if kind == "concept_gap":
        for it in items or []:
            q = (it.get("question") if isinstance(it, dict) else "") or ""
            if any(w in q for w in AGGREGATE_WORDS):
                return _METRIC_GAP_ACTION
    if kind == "columns_or_convention_gap":
        if _scene_hit(items, lambda s: any(
                "no_samples" in _as_list(c.get("flags"))
                for c in _as_list(s.get("trajectory")) if isinstance(c, dict))):
            return _NO_SAMPLES_ACTION
        # `multiturn is True` 是必须前提 —— 单次档轨迹恒空，不是「没调工具」。
        # 判空走 `_as_list`：脏标量与空轨迹同口径处理（不然标量既不触发也不报错、
        # 与 `_scene_digest` 的「按空处理」不一致）。
        if _scene_hit(items, lambda s: s.get("multiturn") is True
                      and _as_list(s.get("trajectory")) == []):
            return _TOOLS_UNUSED_ACTION
    return _ACTIONS.get(kind, _EXEC_OTHER_ACTION)


def _miss_target(call: dict) -> str:
    """一条轨迹 → misses 目标串（设计 §4.4）：`{flag}:{entity}[.{column}]`；
    无 entity 的调用 → `{flag}:{tool}`。"""
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    entity = str(args.get("entity") or "").strip()
    target = entity
    if call.get("tool") == "sample_values" and args.get("column") and entity:
        target = f"{entity}.{args.get('column')}"
    return target or str(call.get("tool") or "?")


def _scene_digest(scene) -> dict | None:
    """失败条目 scene → 提案样本里的现场摘要（设计 §6.2b）；无 / 坏 / 空 scene → None。"""
    if not isinstance(scene, dict) or not scene:
        return None
    digest: dict = {"multiturn": bool(scene.get("multiturn"))}
    anchor = str(scene.get("anchor") or "").strip()
    if anchor:
        digest["anchor"] = anchor
    tools: list[str] = []
    misses: list[str] = []
    for call in _as_list(scene.get("trajectory")):
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
        for flag in _as_list(call.get("flags")):
            if str(flag) == "ok":
                continue
            miss = f"{flag}:{_miss_target(call)}"
            if miss not in misses:
                misses.append(miss)
    if tools:
        digest["tools"] = tools
    if misses:
        digest["misses"] = misses[:6]        # 前 6（有界；设计 §6.2b）
    near = [f"{r.get('entity_name')}({r.get('score') if r.get('score') is not None else '?'})"
            for r in _as_list(scene.get("near_miss")) if isinstance(r, dict)]
    if near:
        digest["near_miss"] = near[:5]
    if scene.get("trajectory_truncated"):
        digest["truncated"] = True
    return digest


def _sample(entry: dict) -> dict:
    """提案样本（设计 §6.2b）：既有三键**一字不动** + 有现场时附 `scene` 摘要。

    观测有 id 时附 `observation_id`（信任闭环 Task 3：候选证据按观测原生 id
    归档）；旧条目/旧测试没有 id，不加这个键，保持原形状。
    """
    sample = {"question": str(entry.get("question") or "")[:200],
              "sql": (entry.get("sql") or entry.get("rejected_sql") or None),
              # reason 取自条目、**不在这里再截**（审计侧已截 200；T 档建议靠它分档）
              "reason": (entry.get("reason") or None)}
    if entry.get("id"):
        sample["observation_id"] = str(entry.get("id"))
    digest = _scene_digest(entry.get("scene"))
    if digest:
        sample["scene"] = digest
    return sample


def _created_at(entry: dict) -> datetime.datetime | None:
    # 时区口径：aware 一律**丢 tz、不换算**（`replace` 而非 `astimezone`），返回恒为 naive。
    # 而 `since`/`until`/`now` 按原样参与比较 —— 传 aware 会在比较处 TypeError，
    # 所以 CLI（任务 3）必须传 **naive** 时间。
    raw = entry.get("created_at")
    if isinstance(raw, datetime.datetime):
        return raw if raw.tzinfo is None else raw.replace(tzinfo=None)
    if isinstance(raw, str):
        try:
            return datetime.datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _scene_rejection_kind(entry: dict) -> str:
    """观测 scene 里的拒绝细分（D1）；无 / 坏 scene → 空串（旧行按原归因走）。"""
    scene = entry.get("scene")
    if not isinstance(scene, dict):
        return ""
    return str(scene.get("rejection_kind") or "").strip()


def attribute(entry: dict) -> dict:
    """一条失败条目 → `{attribution, kind, sub_key}`（认不出的码 → unknown，不猜）。"""
    code = str(entry.get("reason_code") or "").strip()
    # D1：同一 reason_code 下按观测细分拆 kind —— 禁止同查 vs 其它安全闸、
    # 治理 deny vs 掩码列用于谓词/聚合。归因档仍是 T（工具/策略面），不动桶键。
    if code == "rejected_unsafe_sql":
        if _scene_rejection_kind(entry) == "forbidden_pair":
            return {"attribution": "T", "kind": "forbidden_pair",
                    "sub_key": "rejected_unsafe_sql:forbidden_pair"}
    if code == "rejected_sensitive":
        if _scene_rejection_kind(entry) == "mask_position":
            return {"attribution": "T", "kind": "governance_mask_position",
                    "sub_key": "rejected_sensitive:mask_position"}
        return {"attribution": "T", "kind": "governance_policy",
                "sub_key": "rejected_sensitive:policy"}
    if code == "exec_failed":
        blob = f"{entry.get('reason') or ''} {entry.get('sql') or ''}".lower()
        if any(m in blob for m in MISSING_RELATION_MARKERS):
            return {"attribution": "S", "kind": "missing_relation",
                    "sub_key": "exec_failed:missing_relation"}
        return {"attribution": "T", "kind": "exec_other", "sub_key": "exec_failed:other"}
    hit = ATTRIBUTIONS.get(code)
    if hit is None:
        return {"attribution": "unknown", "kind": "unknown_code",
                "sub_key": f"unknown:{code or '(空)'}"}
    return {"attribution": hit[0], "kind": hit[1], "sub_key": code}


def _proposal_id(attribution: str, kind: str, sub_key: str, ds_id: str) -> str:
    """稳定 id：同 (归因|类型|子键|源) ⇒ 同 id ⇒ reject 可跨天生效。"""
    raw = f"{attribution}|{kind}|{sub_key}|{ds_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def diagnose(entries: list[dict] | None = None, *, now: datetime.datetime | None = None,
             since: datetime.datetime | None = None,
             until: datetime.datetime | None = None,
             tenant: str | None = None,
             reject_ids: set[str] | None = None) -> dict:
    """过滤 → 归因 → 聚合成提案。窗口半开 `[since, until)`；`since` 缺省 = 不设下界。

    `entries` 接受 None（→ 空报告），与 `constraints.py` / `evidence.py` 的宽容姿态一致。
    """
    now = now or datetime.datetime.now()
    until = until or now
    reject_ids = reject_ids or set()

    kept: list[dict] = []
    skipped = 0
    for entry in entries or []:
        if not isinstance(entry, dict) or not str(entry.get("reason_code") or "").strip():
            skipped += 1
            continue
        ts = _created_at(entry)
        if ts is None or (since is not None and ts < since) or ts >= until:
            skipped += 1
            continue
        if tenant and str(entry.get("tenant_id") or "") != tenant:
            skipped += 1
            continue
        kept.append(entry)

    groups: dict[tuple, list[dict]] = defaultdict(list)
    buckets = {"C": 0, "S": 0, "T": 0, "unknown": 0}
    for entry in kept:
        got = attribute(entry)
        buckets[got["attribution"]] += 1
        groups[(got["attribution"], got["kind"], got["sub_key"],
                str(entry.get("resource_id") or ""))].append(entry)

    proposals, rejected = [], []
    for (attribution, kind, sub_key, ds_id), items in groups.items():
        proposal = {
            "id": _proposal_id(attribution, kind, sub_key, ds_id),
            "attribution": attribution, "kind": kind, "sub_key": sub_key,
            "data_source_id": ds_id or "(未指定)",
            "count": len(items),
            "samples": [_sample(e) for e in items[:MAX_SAMPLES]],
            "action": _action_for(kind, items),
        }
        (rejected if proposal["id"] in reject_ids else proposals).append(proposal)

    proposals.sort(key=lambda p: (-p["count"], p["id"]))
    rejected.sort(key=lambda p: p["id"])
    return {
        "window": {"since": since.isoformat() if since else None,
                   "until": until.isoformat(), "total": len(kept), "skipped": skipped},
        "buckets": buckets,
        "proposals": proposals,
        "rejected_proposals": rejected,
    }


# ── 采纳（#5 闭环）：把 C 类提案变成抽象层的别名追加 ──────────────

def add_concept_alias(concepts: list, concept_name: str, alias: str) -> dict:
    """给概念加一个别名 —— **采纳通道的写前变换，纯函数、不写任何东西**。

    返回 `{"concepts": <新列表>, "changed": bool, "why": str}`，`why` 四档：

    - `"ok"`：新列表把别名**追加到该概念尾部**（前面的别名位置不动）；
    - `"already_present"`：该概念已有（strip 后比较）→ **幂等 no-op**，别重复写库；
    - `"concept_not_found"`：概念不存在 → **v1 不新建概念**：新建要带源映射（entity+ds），
      形状更深，交给人工在图形页/完整 PUT 里做 —— 诊断口**不猜**；
    - `"alias_taken_by:<concept>"`：**别的**概念已占这个别名 → 拒绝。再加一条就是给
      `ambiguous` 上浇油（那类问题该走 `concept_overlap` 的修法），这里只报告占位者。
    - `"bad_args"`：概念名/别名为空。

    失败分支一律**原样返回入参列表**（调用方据此保证「没变就不写」）。
    """
    name = str(concept_name or "").strip()
    term = str(alias or "").strip()
    if not name or not term:
        return {"concepts": list(concepts or []), "changed": False, "why": "bad_args"}
    out: list = []
    found = False
    for c in concepts or []:
        if not isinstance(c, dict):
            out.append(c)
            continue
        aliases = [str(a) for a in (c.get("aliases") or [])]
        stripped = [a.strip() for a in aliases]
        if str(c.get("name") or "").strip() == name:
            found = True
            if term in stripped:
                return {"concepts": list(concepts or []), "changed": False,
                        "why": "already_present"}
            out.append({**c, "aliases": aliases + [term]})
            continue
        if term in stripped:
            return {"concepts": list(concepts or []), "changed": False,
                    "why": f"alias_taken_by:{c.get('name')}"}
        out.append(c)
    if not found:
        return {"concepts": list(concepts or []), "changed": False,
                "why": "concept_not_found"}
    return {"concepts": out, "changed": True, "why": "ok"}


def add_metric_alias(metrics: list, metric_name: str, alias: str) -> dict:
    """给指标加一个别名 —— **采纳通道的写前变换，纯函数、不写任何东西**。

    返回 `{"metrics": <新列表>, "changed": bool, "why": str}`，`why` 五档（与
    `add_concept_alias` 逐档同构）：

    - `"ok"`：新列表把别名**追加到该指标尾部**（前面的别名位置不动）；
    - `"already_present"`：该指标已有（strip 后比较）→ **幂等 no-op**，别重复写库；
    - `"metric_not_found"`：指标不存在 → **不新建指标**（新建要带落点通道，形状更深，
      交给画布「指标」面板或完整 PUT）—— 诊断口**不猜**；
    - `"alias_taken_by:<名>"`：**别的**指标已占这个别名 → 拒绝（与实体档同理）。
    - `"bad_args"`：指标名/别名为空。

    失败分支一律**原样返回入参列表**（调用方据此保证「没变就不写」）。查重范围**只在
    metrics 同族内**：实体别名与指标别名同名词面合法共存（不判跨族冲突）。
    """
    name = str(metric_name or "").strip()
    term = str(alias or "").strip()
    if not name or not term:
        return {"metrics": list(metrics or []), "changed": False, "why": "bad_args"}
    out: list = []
    found = False
    for m in metrics or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        aliases = [str(a) for a in (m.get("aliases") or [])]
        stripped = [a.strip() for a in aliases]
        if str(m.get("name") or "").strip() == name:
            found = True
            if term in stripped:
                return {"metrics": list(metrics or []), "changed": False,
                        "why": "already_present"}
            out.append({**m, "aliases": aliases + [term]})
            continue
        if term in stripped:
            return {"metrics": list(metrics or []), "changed": False,
                    "why": f"alias_taken_by:{m.get('name')}"}
        out.append(m)
    if not found:
        return {"metrics": list(metrics or []), "changed": False,
                "why": "metric_not_found"}
    return {"metrics": out, "changed": True, "why": "ok"}
