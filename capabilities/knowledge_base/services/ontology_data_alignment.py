#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""跨模态对齐（P1.5b）：建议生成 + 人审状态流转。

链路：文档侧 TBox 实体类型（KB 级 compiled schema 的 `entity_types[].name`）
↔ 抽象 schema 概念（`entities[].sources[{data_source_id, entity_name}]`）
↔ 数据源实体（per-source compiled schema 的 `entity_types[].name → source_object_id`）。

- **建议只写 `proposed`**：同名/别名命中不等于人已确认；`active` 才进投影
  （Task 3 的 `MAPS_TO`），`rejected` 保留行避免下次重新提出同一条。
- **同名判定复用同义词归一器**（`synonym_normalizer._normalize_key`）：整条链路
  必须是同一把尺子（全角/大小写/首尾空白），否则「同义词能归并、对齐建议却认不出」
  这类口径分叉只会静默漏建议。
- **建议是幂等的**：生成前先读该 KB 的全部对齐行（**任何状态**）—— 已存在的
  三元组只计 `existing`，不再 upsert（尤其不能把 `rejected` 翻回 `proposed`）。

错误码口径（P1.5b）：非法参数/状态用 **1001**，行或 KB 缺失用 **1002**；
本线全程不产生 1003/409。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ..models.ontology_data_alignment import validate_status
from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.ontology_abstract_schema_repository import (
    OntologyAbstractSchemaRepository,
)
from ..repository.ontology_data_alignment_repository import (
    OntologyDataAlignmentRepository,
)
from ..repository.ontology_schema_repository import OntologySchemaRepository
# 模块级 import 投影（无环：投影只依赖 models/repos，不 import 本服务）。
# 调用仍走**模块属性**而不是 `from ... import 函数`：触发点单测替换 helper 时，
# 晚绑定才看得见替换（按函数名 import 会把引用钉死在 import 那一刻）。
from . import ontology_data_projection
# 新鲜度判据必须与 KB 级 compiled schema 的读取契约（Task 1）同一把尺子：
# 有存储行看 `schema_mode`/`sync_status`，无存储行看有没有可用 schema。
from .ontology_compiler import (
    OntologyCompiler,
    resolve_freshness,
    stored_entity_type_names,
)
# ⚠️ 有意复用**私有**函数：跨模态对齐与同义词归并必须是同一套归一（全角→半角、
# casefold、trim）。在这里抄一份「看起来一样」的实现，下次归一规则调整时两边
# 就会分叉，而分叉的症状是「建议莫名少了」，不会报错。
from .synonym_normalizer import _normalize_key

logger = logging.getLogger(__name__)


def normalize_name(value: Any) -> str:
    """对齐匹配用的归一键：trim + 全角→半角 + casefold。

    空值/非字符串一律归成 `""`（调用方据此跳过空名，不参与匹配）。
    """
    return _normalize_key(str(value or ""))


def _text(value: Any) -> str:
    """只要 `str(...).strip()`：保留原始大小写（落库与输出用）。"""
    return str(value or "").strip()


def build_suggestions(
    doc_types: list[str],
    concepts: list[dict],
    source_entities: dict[tuple[str, str], str],
) -> list[dict]:
    """同名/别名匹配 → 候选对齐三元组（纯函数，无 IO）。

    Args:
        doc_types: 文档侧实体类型名（KB 级 compiled schema `entity_types[].name`）。
        concepts: 抽象 schema 的概念，形状
            `{name, aliases, sources: [{data_source_id, entity_name}]}`。
        source_entities: 各源 compiled schema 的 `{(ds_id, entity_name): 物理表}`。

    规则：`normalize(doc_type)` **命中任一** 即算匹配 ——
    `concept.name`、任一 `alias`、或该概念的某个 `sources[].entity_name`。
    （「任一」而不是「全部」：别名存在的意义就是让只有别名对得上的情况也能命中。）

    **显式排除的落点不参与**：`sources[].included is False` 是人否决过的落点，
    编译投影（`ontology_compiler.abstract_to_compiled_types`）会丢弃它；这里
    照收就会把它重新提议成 `proposed`，审批后甚至挂上 `MAPS_TO`。缺省/`true`
    视为纳入（向后兼容没有该字段的旧数据）。

    产出 `{doc_type, data_source_id, entity_name, table}`：

    - 只保留能解析到物理表的候选（`source_entities` 里查不到 = 未解析，不产出）；
    - 按 `(doc_type, data_source_id, entity_name)` 去重，**保序**；
    - 输出的三个业务键已 `strip()`，与仓储写入口径一致（`" Asset "` 与 `"Asset"`
      落的是同一行，第二次生成才不会被当成新候选）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_doc_type in doc_types or []:
        doc_type = _text(raw_doc_type)
        doc_key = normalize_name(doc_type)
        if not doc_key:
            continue
        for concept in concepts or []:
            if not isinstance(concept, dict):
                continue
            match_keys = {normalize_name(concept.get("name"))}
            for alias in concept.get("aliases") or []:
                match_keys.add(normalize_name(alias))
            for source in concept.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                # `included: false` = 人工显式排除的落点。编译投影会丢弃它
                # （`abstract_to_compiled_types` 同口径），建议生成也必须跳过。
                if source.get("included") is False:
                    continue
                ds_id = _text(source.get("data_source_id"))
                entity_name = _text(source.get("entity_name"))
                if not (ds_id and entity_name):
                    continue
                # `doc_type` 直接等于数据源实体名也算命中（概念名/别名与它无关时）。
                if doc_key not in match_keys and doc_key != normalize_name(entity_name):
                    continue
                table = _text(source_entities.get((ds_id, entity_name)))
                if not table:
                    continue
                triple = (doc_type, ds_id, entity_name)
                if triple in seen:
                    continue
                seen.add(triple)
                out.append({
                    "doc_type": doc_type,
                    "data_source_id": ds_id,
                    "entity_name": entity_name,
                    "table": table,
                })
    return out


class OntologyDataAlignmentService:
    """对齐建议的生成 / 列举 / 审批。无状态，每次调用自己开会话。

    `session_factory` 只为单测注入；生产走 `get_db_session`。
    """

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_db_session
        # ⚠️ 本服务在部署里是**进程级单例**（capability 只构造一次）：
        # 不允许挂任何跨 await 的实例可变状态 —— 并发请求会在 await 之间互相
        # 覆盖（freshness / stale 串味）。所有中间结果只在方法内用局部变量传递。

    async def _ensure_kb(self, session, kb_id: str, tenant_id: str) -> None:
        """KB 必须存在且属于该租户（否则 1002/404）。

        少了这一句：`list` 对不存在/他人的 KB 回空列表（200，与「没有对齐」同形）；
        `generate` 则会对着一个不存在的 KB 一路返回 0/0/0，而 `get_compiled_schema`
        的 YAML 兜底甚至会给它编出一份 schema —— 错误被彻底吞掉。
        """
        await KnowledgeInfoRepository(session).get_required(kb_id, tenant_id)

    # ── 读取输入（各方法拆开是为了单测可插桩，生产行为不变）──────────────

    async def _load_compiled_rows(self, session, tenant_id: str, kb_id: str):
        """该 KB 的全部 active compiled schema 行（纯查询，不缓存）。

        缓存会变成实例可变状态（本服务是进程级单例，见 `__init__`）。
        `generate` / `list` 各自在入口查一次，再把 rows 显式传给
        `_load_doc_types_state` / `_load_source_entities`，避免同一份数据多次
        往返，又不跨请求共享。
        """
        return await OntologySchemaRepository(session).list_compiled_schemas(
            tenant_id, kb_id)

    async def _load_doc_types_state(
        self, session, tenant_id: str, kb_id: str, rows=None,
    ) -> tuple[list[str], str, bool]:
        """文档侧实体类型名 + 新鲜度 + 是否来自存储行（无实例状态）。

        **优先读已落库的知识库级行**（`data_source_id IS NULL`），**不能**直接走
        `OntologyCompiler().get_compiled_schema()`：KB 有 active 抽象 schema 时，
        后者返回的是**抽象投影**，实体名是中文概念名（如「资产」）；而文档 ABox
        实体的 `canonical_name` 存的是知识库级 compiled schema 的实体名（如
        `Asset`）。拿投影名去匹配，一条 `MAPS_TO` 都投不出来。

        只有库里没有知识库级行时才回落到编译器（旧库 / YAML 兜底路径 —— 这些
        场景没有落库行，编译器结果是唯一可用的实体名来源）。回落必须
        `auto_compile=False`：`list` 是只读接口，`generate` 也受全局约束
        「不自动重编译」——两者都不能因为读一次对齐就顺手写一份 compiled
        schema（编译器默认 `auto_compile=True` 会落库；旧库在 generate 时
        不再顺手编译落库）。

        ⚠️ 回落结果必须再验一次 `source_type`：KB 有 active 抽象 schema 时，
        `get_compiled_schema` 返回的是 `source_type == "abstract"` 的**抽象
        投影**（实体名是「资产」这类中文概念名）。投影名与文档 ABox 的
        `canonical_name`（`Asset`）不同源 —— 一旦拿它生成建议，就是把概念名
        写成 doc_type，正是 `49594fa3` 修掉的那个错配。遇到抽象投影一律
        当作「没有文档类型」（宁可少建议，不可错建议）。
        """
        if rows is None:
            rows = await self._load_compiled_rows(session, tenant_id, kb_id)
        stored = next(
            (row for row in rows
             if not _text(getattr(row, "data_source_id", None))),
            None,
        )
        if stored is not None:
            # 实体名口径与 Task 1 的 `stored_entity_type_names` 同一实现：
            # 「存储行上有什么」只有一处定义（含非 dict 过滤与 strip）。
            freshness = resolve_freshness(stored, None)
            # I-3：seeded 还要和当前模板 structure_hash 比对（YAML 兜底无期望
            # hash ⇒ 保持 seeded）。manual_edited / outdated 已经定性，不再白查
            # 一次 binding + 模板场景。
            if freshness == "seeded":
                binding = await OntologySchemaRepository(session).get_binding(
                    tenant_id, kb_id)
                expected_hash = await OntologyCompiler()._expected_source_hash(
                    session, tenant_id, binding)
                freshness = resolve_freshness(
                    stored, None, expected_hash=expected_hash)
            return (
                stored_entity_type_names(stored),
                freshness,
                True,
            )
        schema = await OntologyCompiler().get_compiled_schema(
            tenant_id, kb_id, auto_compile=False)
        if not isinstance(schema, dict):
            return [], "missing", False
        freshness = resolve_freshness(None, schema)
        # 抽象投影（`source_type == "abstract"`）不能当文档侧实体名：
        # 见 docstring。缺 `source_type` 的旧形状/兜底结果照旧使用。
        if _text(schema.get("source_type")) == "abstract":
            return [], freshness, False
        names: list[str] = []
        for entity in schema.get("entity_types") or []:
            if isinstance(entity, dict):
                name = _text(entity.get("name"))
                if name:
                    names.append(name)
        return names, freshness, False

    async def _load_doc_types(
        self, session, tenant_id: str, kb_id: str, rows=None,
    ) -> list[str]:
        """兼容薄包装：只返回实体名（判定逻辑见 `_load_doc_types_state`）。"""
        names, _freshness, _from_stored = await self._load_doc_types_state(
            session, tenant_id, kb_id, rows=rows)
        return names

    async def _load_concepts(self, session, tenant_id: str,
                             kb_id: str) -> list[dict]:
        """active 抽象 schema 的概念（`schema.entities`）。

        **只认 `active`**：`proposed` 是 LLM 候选、还没人确认；拿它生成对齐建议
        等于把未确认的概念层当成事实（同 ② 只消费 active 的口径）。
        """
        row = await OntologyAbstractSchemaRepository(session).get_by_kb(
            tenant_id, kb_id)
        if row is None or (getattr(row, "status", None) or "proposed") != "active":
            return []
        schema = getattr(row, "schema", None)
        entities = schema.get("entities") if isinstance(schema, dict) else None
        if not isinstance(entities, list):
            return []
        return [entity for entity in entities if isinstance(entity, dict)]

    async def _load_source_entities(
        self, session, tenant_id: str, kb_id: str, rows=None,
    ) -> dict[tuple[str, str], str]:
        """各数据源 active compiled schema 的 `(ds_id, 实体名) → 物理表`。

        `list_compiled_schemas` 同时返回知识库级那份（`data_source_id=None`）——
        它不属于任何源，必须跳过；否则 `(None, name)` 这样的键永远解析不到，
        徒增噪声。
        """
        if rows is None:
            rows = await self._load_compiled_rows(session, tenant_id, kb_id)
        out: dict[tuple[str, str], str] = {}
        for row in rows:
            ds_id = _text(getattr(row, "data_source_id", None))
            if not ds_id:
                continue
            for entity in getattr(row, "entity_types", None) or []:
                if not isinstance(entity, dict):
                    continue
                name = _text(entity.get("name"))
                table = _text(entity.get("source_object_id"))
                if name and table:
                    out[(ds_id, name)] = table
        return out

    @staticmethod
    def _with_stale(item: dict, doc_types: set[str], freshness: str,
                    from_stored: bool) -> dict:
        """逐行标注 doc_type 是否已不在当前文档侧实体名集合里。

        `stale` 是三态，判定前提是实体名**来自存储的 KB 级行**：
        YAML 兜底 / 抽象投影 / missing 没有可比对的存储集合，一律 `None`
        （不虚构过期，不删除对齐行；与 Task 1 的 `get_kb_schema_status` 同口径）。

        M-2：只有 `active` / `proposed` 参与判定。`rejected` 是人已否决的候选，
        不标 stale、不计影响面（行本身保留）。
        """
        if (item.get("status") or "") not in ("active", "proposed"):
            return {**item, "stale": None}
        if not from_stored or freshness not in (
                "seeded", "outdated", "manual_edited"):
            return {**item, "stale": None}
        return {**item, "stale": _text(item.get("doc_type")) not in doc_types}

    # ── 对外接口 ────────────────────────────────────────────────────

    async def list(self, tenant_id: str, kb_id: str,
                   status: Optional[str] = None) -> dict:
        """该 KB 的对齐行（可按状态过滤；非法状态由仓储/白名单抛 1001）。

        返回 `{items, freshness}`；每项在原行上追加 `stale: bool | None`。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._session_factory() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            rows = await OntologyDataAlignmentRepository(session).list_by_kb(
                tenant_id, kb_id, status)
            compiled = await self._load_compiled_rows(session, tenant_id, kb_id)
            doc_types, freshness, from_stored = await self._load_doc_types_state(
                session, tenant_id, kb_id, rows=compiled)
            doc_type_set = set(doc_types)
            items = [
                self._with_stale(row.to_dict(), doc_type_set, freshness,
                                 from_stored)
                for row in rows
            ]
            return {"items": items, "freshness": freshness}

    async def generate(self, tenant_id: str, kb_id: str,
                       *, created_by: Optional[str] = None) -> dict:
        """生成建议并只落 `proposed`。

        返回 `{created, existing, unresolved, freshness, stale}`：

        - `created`：本次新插入的三元组数；
        - `existing`：已有任意状态行的三元组数（含 `rejected` —— 人否过的建议
          不该每次生成都重新冒出来）；
        - `unresolved`：**一个可解析候选都没有**的文档实体类型数（UI 据此显式
          展示「未对齐」；未对齐 ≠ 没有，见设计 §四）；
        - `freshness`：KB 级 compiled schema 的新鲜度（读时判定）；
        - `stale`：`freshness == "outdated"`（需重编译，UI 显式提示）；
        - `reason`：机器可读原因，过期时为 `kb_schema_outdated`，否则 None。
        """
        tenant_id = require_tenant(tenant_id)
        async with self._session_factory() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            rows = await self._load_compiled_rows(session, tenant_id, kb_id)
            doc_types, freshness, _from_stored = await self._load_doc_types_state(
                session, tenant_id, kb_id, rows=rows)
            concepts = await self._load_concepts(session, tenant_id, kb_id)
            source_entities = await self._load_source_entities(
                session, tenant_id, kb_id, rows=rows)
            suggestions = build_suggestions(
                doc_types, concepts, source_entities)

            repo = OntologyDataAlignmentRepository(session)
            existing_rows = await repo.list_by_kb(tenant_id, kb_id)
            # **任何状态**都算已存在：只查 proposed 的话，一条 rejected 行会被
            # 重新 upsert 成 proposed（否过又冒出来），active 也不会被计数。
            existing_triples = {
                (_text(row.doc_type), _text(row.data_source_id),
                 _text(row.entity_name))
                for row in existing_rows
            }

            created = existing = 0
            for suggestion in suggestions:
                triple = (
                    suggestion["doc_type"],
                    suggestion["data_source_id"],
                    suggestion["entity_name"],
                )
                if triple in existing_triples:
                    existing += 1
                    continue
                await repo.upsert(
                    tenant_id, kb_id,
                    suggestion["doc_type"], suggestion["data_source_id"],
                    suggestion["entity_name"],
                    status="proposed", created_by=created_by,
                )
                # 防批内重复：`build_suggestions` 已按三元组去重，这里登记是给
                # 「万一将来去重键放宽」留的幂等护栏（重复 upsert 会白烧一次写）。
                existing_triples.add(triple)
                created += 1

            distinct_doc_types = {
                _text(doc_type) for doc_type in doc_types
                if _text(doc_type)
            }
            resolved_doc_types = {s["doc_type"] for s in suggestions}
            return {
                "created": created,
                "existing": existing,
                "unresolved": len(distinct_doc_types - resolved_doc_types),
                "freshness": freshness,
                "stale": freshness == "outdated",
                # M-3：给前端一个稳定的机器可读原因（只有过期才有值）。
                "reason": (
                    "kb_schema_outdated" if freshness == "outdated" else None
                ),
            }

    async def set_status(self, tenant_id: str, kb_id: str,
                         alignment_id: str, status: str) -> dict:
        """人审流转：proposed / active / rejected。

        - 非法状态在**查库前**抛 1001（仓储 `set_status` 也校验，这里是更早一层）；
        - 行不存在（或不属于该租户/KB）→ 1002；
        - **任何**状态更新之后都 best-effort 重建 `MAPS_TO` 投影 —— 投影是
          增能项，失败只告警，**不改变审批结果**。

        ⚠️ 只在新状态是 `active|rejected` 时重建会留下陈旧边：`active → proposed`
        的撤审 PATCH 不清图，旧 `MAPS_TO` 会一直挂着。重建本身是**全量
        delete+rebuild**，所以 `proposed`/`rejected` 传空期望集正好清边、`active`
        重挂，三个状态走同一条路即可。

        ⚠️ 投影调用必须放在会话退出（commit）**之后**：投影另开会话读对齐行，
        事务未提交时它读到的还是旧状态。
        """
        tenant_id = require_tenant(tenant_id)
        validate_status(status)
        async with self._session_factory() as session:
            await self._ensure_kb(session, kb_id, tenant_id)
            row = await OntologyDataAlignmentRepository(session).set_status(
                tenant_id, kb_id, alignment_id, status)
            if row is None:
                raise ResourceNotFoundError(
                    message=f"跨模态对齐记录不存在：{alignment_id}",
                    details={"alignment_id": alignment_id,
                             "knowledge_base_id": kb_id},
                )
            result = row.to_dict()

        # 三态一律重建（不是只在 active|rejected 时）：见 docstring 的陈旧边说明。
        await self._rebuild_projection(tenant_id, kb_id)
        return result

    async def _rebuild_projection(self, tenant_id: str, kb_id: str) -> None:
        """best-effort 重建对齐投影（`rebuild_alignments_best_effort`）。

        投影 helper 自身吞异常；这里再兜一层是触发点的统一口径 —— 投影是增能项，
        任何失败都只告警，不影响审批结果的结局。
        """
        try:
            await ontology_data_projection.rebuild_alignments_best_effort(
                tenant_id, kb_id)
        except Exception as exc:  # noqa: BLE001 —— 投影是增能项
            logger.warning("对齐投影失败（不影响审批结果）：kb=%s err=%s",
                           kb_id, exc)


__all__ = [
    "OntologyDataAlignmentService",
    "build_suggestions",
    "normalize_name",
]
