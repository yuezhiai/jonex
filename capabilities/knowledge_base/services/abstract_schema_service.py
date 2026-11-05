#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""跨数据源的抽象 schema（概念对齐层）服务。

三件事：

- **生成**（`generate`）：读该库**全部数据源级** schema 的 entity_types →
  交给 LLM 提候选概念簇 → 迁移成 v2 schema（概念 = 实体）落 `status='proposed'`。
  未确认不参与检索（决策 4）。
- **读取**（`get`）：读出对齐，并**读时算标脏**（决策 7）。
- **保存/确认**（`save`）：界面上的改名/合并/删源/确认都走它，带乐观锁；
  入参与落库形状都是 v2 的 `{entities, relations}`。

**不做什么**：不写 `ontology_synonyms`（那是「等价词归一」，与「跨源概念映射」
不是一回事）；不改检索召回（消费侧只产候选 + 打标注，见 `concept_expansion`）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
import uuid
from typing import NamedTuple, Optional

from sqlalchemy import delete, func, insert, select

from jonex_core.common.cache import CacheUtil, get_redis_client
from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import (
    InvalidParameterError,
    RateLimitExceededError,
    ResourceConflictError,
    ResourceNotFoundError,
    UpstreamServiceError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.ontology_llm import (
    align_document_landings,
    propose_concept_clusters,
    resolve_focus_entities,
    suggest_attribute_display_names,
)
from jonex_core.common.tenant import require_tenant

from ..models.ontology_abstract_schema import AbstractSchemaSaveHistory
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from ..repository.ontology_abstract_schema_repository import (
    OntologyAbstractSchemaRepository,
)
from ..repository.ontology_schema_repository import OntologySchemaRepository
from .abstract_construct import (
    apply_alias_hints_to_entities,
    apply_llm_landings,
    attach_landings_by_name,
    build_edge_suggestion,
    build_landing_map,
    build_source_bundles,
    collect_edge_scores,
    cross_source_candidates,
    document_seed_from_compiled,
    lifted_relations,
    merge_duplicate_source_entities,
    merged_attributes_for_entity,
    needs_display_name,
    parse_metric_hints,
    run_name_stage,
)
from .abstract_focus import (
    FOCUS_SEEDS_MAX,
    FOCUS_SEED_SOFT_WARN,
    build_relation_graph,
    expand_focus_closure,
    filter_focus_seeds,
    validate_focus_seeds,
)
from .concept_expansion import (
    MAX_ATTRIBUTES_PER_ENTITY,
    MAX_LANDING_SLOT_CHARS,
    MAX_METRICS,
    MAX_RELATIONS,
    _clean_str,
    filter_dangling_sources,
    migrate_v1_concepts,
    normalize_concepts,
    normalize_schema,
)
from .evidence_probe import probe_distinct
from . import tbox_import

logger = logging.getLogger(__name__)

ABSTRACT_STATUSES = ("proposed", "active")

#: 每个 (租户, 知识库) 保留的保存快照条数（spec §2 保留策略）。
_SNAPSHOT_RETENTION = 20

#: 历史列表的默认条数与上限（spec §4：默认 20，上限 50）。
_SNAPSHOT_LIST_DEFAULT = 20
_SNAPSHOT_LIST_MAX = 50


def _require_snapshot_kb(kb_id) -> str:
    """详情/还原**必须**带 KB：空串/None 一律 1001，绝不退化成「只按租户查」。

    只按租户过滤等于把「拿别人的 snapshot_id 试探」从 1002 变成可能命中；
    能力层的 `_require_nonblank` 是第一道，这里是服务层自己的口（内部链路
    直接调服务时同样生效）。
    """
    value = str(kb_id or "").strip()
    if not value:
        raise InvalidParameterError(message=translate(
            "err.common.missing_param",
            params={"key": "knowledge_base_id"},
            fallback="缺少必填参数：knowledge_base_id",
        ))
    return value


def _clamp_snapshot_limit(limit) -> int:
    """把外部传入的 `limit` 收敛到 1..50；非整数/缺省回落到默认 20。

    网关 query 已经声明了类型，但能力动作可以被内部链路直接 invoke（不带
    FastAPI 校验），所以服务层必须自己兜 —— 负数和 `None` 都不能漏到 SQL。
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = _SNAPSHOT_LIST_DEFAULT
    return max(1, min(value, _SNAPSHOT_LIST_MAX))


def _source_hash(source_entities: list[dict]) -> str:
    """各源 (ds, entity_name) 的指纹 —— 供界面判断「源变没变，要不要重生成」。

    只吃**组合**、不吃描述文本：改一句 entity 描述也会让指纹变，但那种变化
    对「要不要重生成对齐」没有信息量，反而会天天提示。
    """
    pairs = sorted(
        (e.get("data_source_id", ""), e.get("entity_name", ""))
        for e in source_entities
    )
    blob = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _count_attrs(entities) -> int:
    total = 0
    for ent in entities or []:
        if isinstance(ent, dict):
            total += len([a for a in (ent.get("attributes") or []) if isinstance(a, dict)])
    return total


#: 截断类 warning 的条数上限（不刷屏；超出只留一条合计）。
_MAX_CAP_WARNINGS = 5


def _fed_unique_attribute_counts(entities) -> dict[str, int]:
    """`{实体名: 喂入的去重属性名数}`（合并键与 `normalize_schema` 同口径：精确名去空白）。"""
    buckets: dict[str, set[str]] = {}
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        bucket = buckets.setdefault(name, set())
        for attr in _list_field(ent.get("attributes")):
            if isinstance(attr, dict):
                attr_name = str(attr.get("name") or "").strip()
                if attr_name:
                    bucket.add(attr_name)
    return {name: len(bucket) for name, bucket in buckets.items()}


def _append_cap_warnings(warnings: list[str], schema: dict,
                         fed_entities, fed_relations, fed_metrics) -> None:
    """把 `normalize_schema` 的**静默截断**显形（spec §四/§五「上限截断 + warning」）。

    - 属性：按实体比对「喂入的去重属性名数」与落库属性数（同名合并已先折叠掉，
      差值 = 撞了 `MAX_ATTRIBUTES_PER_ENTITY` 被丢的条数）；
    - 关系：喂入去重关系名数 vs 落库关系数（差值 = 被上限截断或两端未落点），
      另有「落库数已达上限」的单列一条。
    - 指标：喂入去重指标名数超上限时记一条（差值 = 被 `MAX_METRICS` 截断的条数）。
    条数封顶 `_MAX_CAP_WARNINGS`（先例：探针失败 warning 同款纪律），超出记一条合计。
    """
    truncated: list[str] = []
    fed_attrs = _fed_unique_attribute_counts(fed_entities)
    for ent in _list_field(schema.get("entities")):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        fed = fed_attrs.get(name, 0)
        got = len(_list_field(ent.get("attributes")))
        if fed > got:
            truncated.append(
                f"实体「{name}」属性超过上限 {MAX_ATTRIBUTES_PER_ENTITY}，截去 {fed - got} 个"
            )
    for line in truncated[:_MAX_CAP_WARNINGS]:
        warnings.append(line)
    if len(truncated) > _MAX_CAP_WARNINGS:
        warnings.append(
            f"另有 {len(truncated) - _MAX_CAP_WARNINGS} 个实体的属性被截断（明细从略）"
        )

    fed_rels = {
        str(r.get("name") or "").strip()
        for r in (fed_relations or [])
        if isinstance(r, dict) and str(r.get("name") or "").strip()
    }
    kept = len(_list_field(schema.get("relations")))
    if kept >= MAX_RELATIONS:
        warnings.append(
            f"关系数达到上限 {MAX_RELATIONS}，超出部分被截断"
            f"（喂入去重后 {len(fed_rels)} 条，落库 {kept} 条）"
        )
    elif len(fed_rels) > kept:
        warnings.append(
            f"部分关系未落库（喂入去重后 {len(fed_rels)} 条，落库 {kept} 条；"
            "两端未落点或被上限截断）"
        )

    fed_metric_names = {
        str(m.get("name") or "").strip()
        for m in (fed_metrics or [])
        if isinstance(m, dict) and str(m.get("name") or "").strip()
    }
    kept_metrics = len(schema.get("metrics") or [])
    if len(fed_metric_names) > MAX_METRICS:
        warnings.append(
            f"指标超过上限 {MAX_METRICS}，截去 {len(fed_metric_names) - kept_metrics} 个"
        )


#: 生成期在飞守卫的 TTL（秒）。它是**崩溃兜底**：进程被杀 / 副本被打死时键最多留
#: 这么久；正常路径（成功、失败、抛异常）都走 `finally` 里的显式 DEL。
GENERATE_GUARD_TTL_SECONDS = 300


def _generate_guard_key(tenant_id: str, kb_id: str) -> str:
    """守卫键：按 **(租户, 知识库)** 分 —— 两个租户生成同名知识库不该互相挡。"""
    return f"abstract_schema:gen:{tenant_id}:{kb_id}"


class _GenerateGuard(NamedTuple):
    """占住的生成位：`key` 是锁键，`lock_id` 是本次生成写进键里的值。

    `lock_id` 只为一件事存在 —— **释放时比对**。一次生成 ~40 秒，长过 TTL（或
    进程卡顿）时键会先过期、后面的生成能拿到同一把键；那时晚到的 `finally` 若
    无条件 DEL，删掉的是**别人**的锁，第三个请求就能和第二个并发生成 —— 守卫
    从「挡住成本」退化成「挡不住」。所以键里存的必须是每次生成独有的值。
    """

    key: str
    lock_id: str


async def _acquire_generate_guard(
    tenant_id: str, kb_id: str,
) -> tuple[bool, Optional[_GenerateGuard]]:
    """试着占住「该 (租户, 知识库) 正在生成」这个位置。

    一次生成 ~40 秒、最多 3.2 万输出 token（实测 ~1.7 万）。界面的 loading
    只挡得住一个标签页里的连点；两个标签页、两个人、一个脚本就是 N 次并发。
    正确性本来就有人管（`upsert` 的 CAS 会把输的那次判成 1003），所以要挡的是
    **成本**：并发的第二次连 LLM 都不该进。

    Returns:
        `(True, guard)` 占住了 —— 调用方**必须**在 `finally` 里
                        `_release_generate_guard(guard)`。
        `(False, None)` 已经有一次生成在飞 —— 调用方立刻拒掉本次（429）。
        `(True, None)`  连 Redis 客户端都没建起来 —— **fail open**：键必然没落，
                        照常生成，没有键要释放。

    fail open 的理由（这条是刻意的，不是漏判）：它是**成本守卫**、不是正确性守卫。
    数据的最终一致性由 `upsert` 的 CAS 兜底（并发的输家撞 1003，谁都不会拿到
    半份对齐），而一次缓存抖动若能把整个功能打成 500/429，代价远大于多花一次
    token —— 缓存是**降级项**，不是生成的前置依赖。
    """
    key = _generate_guard_key(tenant_id, kb_id)
    guard = _GenerateGuard(key, uuid.uuid4().hex)
    try:
        client = get_redis_client()
    except Exception as exc:  # noqa: BLE001 —— 客户端都建不起来，键必然没落
        logger.warning(
            "生成守卫不可用（Redis 故障），本次生成放行: kb=%s err=%s", kb_id, exc,
        )
        return True, None
    try:
        # SET NX EX：占位与「只在没人占时才成功」是**一条**原子命令；
        # 分开写（先 EXISTS 再 SET）就是典型的竞态。值存 lock_id 供释放时比对。
        acquired = await client.set(
            key, guard.lock_id, nx=True, ex=GENERATE_GUARD_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        # **歧义**失败：连得上、却抛在 SET 上（超时 / 回包丢了），键落没落不知道。
        # 按「可能占住了」把 guard 交回调用方去释放 —— 键真落了就清掉，不留成
        # 300 秒幽灵锁（这个库接下来每次生成都会 429，而实际什么都没在跑）；
        # 没落就是一次空操作。fail open 的结论不变：照常生成。
        logger.warning(
            "生成守卫占位结果未知（Redis 故障），仍按已占位处理并释放: kb=%s err=%s",
            kb_id, exc,
        )
        return True, guard
    finally:
        # 「占没占住」与「关不关得上客户端」是两件事：`aclose` 失败**不得**改写
        # 上面的判定。旧写法把两者塞进同一个 try，于是一次收尾抖动就把「已占位」
        # 吞成「fail open、无键可放」，键留在 Redis 里白锁这个 KB 300 秒。
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "生成守卫客户端关闭失败（不影响本次占位判定）: kb=%s err=%s", kb_id, exc,
            )
    if not acquired:
        logger.info("已有生成在飞，拒绝并发生成: kb=%s", kb_id)
        return False, None
    return True, guard


async def _release_generate_guard(guard: _GenerateGuard) -> None:
    """**比对后**释放：只有键里的值还是本次生成的，才删。

    比较删除是原子的 —— 直接用仓内现成原语 `CacheUtil.release_lock`（Lua：
    GET == 本次 lock_id 才 DEL），不自己拼 eval，也就没有「先 GET 再 DEL」
    那个中间被插队的窗口。

    **绝不把异常再抛出去**：释放失败发生在一次生成**已经成功之后**，为此把一个
    成功的结果打成失败是本末倒置 —— 代价只是这个 KB 最多再等 300 秒（TTL）。
    """
    try:
        released = await CacheUtil.release_lock(guard.key, guard.lock_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "生成守卫释放未确认（Redis 故障，最坏等 TTL 过期）: key=%s err=%s",
            guard.key, exc,
        )
        return
    if not released:
        # 键已不在（正常过期），或已经不是我们的（过期后别人又占住了）——
        # 两种都不该由本次生成来删。
        logger.info("生成守卫已不属于本次生成，跳过删除: key=%s", guard.key)


def _count_sources(concepts) -> int:
    """数 concepts 里的源映射条数 —— `dropped_sources` 那个差值的两侧都走它。

    非 list 的 `sources` 记 0，**不抛**：模型回 `{"sources": 42}` 时 `len(42)` 是
    `TypeError`，它不是 `JonexException`、全局处理器没有码可翻，会把一次
    「容忍垃圾 LLM 输出」的生成本该走完的路打成 500。记 0 与下游口径一致 ——
    脏的那条在 `filter_dangling_sources` 眼里同样没有源（`_as_list` 收敛成空），
    两侧都不计，差值才仍然表示「剔掉了几条」。
    """
    total = 0
    for c in (concepts or []):
        if not isinstance(c, dict):
            continue
        srcs = c.get("sources")
        if isinstance(srcs, list):
            total += len(srcs)
    return total


def _list_field(value) -> list:
    """「本该是 list」的容器字段收敛成 list，绝不抛。

    标脏（`_annotate_schema_staleness`）是 **GET 的用户读路径**，输入是库里的
    JSONB —— 可能是人手改过的。被替换掉的旧实现在这类形状上是宽容的，新的不许
    更差：一个畸形容器（非 list / 元素非 dict）不该把用户的整次读打成 500
    （`TypeError` 不是 `JonexException`、没有码可翻）。只认 list：JSONB 里
    落不出 tuple/set，其它一律当空。
    """
    return value if isinstance(value, list) else []


def _assert_attribute_caps(schema: dict) -> None:
    """保存前的属性上限**前置闸**（缺口修复：静默截断 → 显式 1001）。

    `normalize_schema` 对超上限的实体属性是**静默截断**（`_merge_attributes`
    遇 `>= MAX_ATTRIBUTES_PER_ENTITY` 直接跳过新属性）——UI 保存路径据此会
    「点保存、回成功、属性却没进库」，无任何提示。这里在 normalize **之前**
    按「实体名去重后的属性名集合」预检：任一实体超出上限 → 抛 1001。
    （口径与导入器 `tbox_import._check_merged_limits` 的属性档同源：宁可拒绝、
    不静默丢；本闸只查**单份输入自身**的超限，跨侧合并越顶由导入器预检管。）
    """
    for ent in _list_field(schema.get("entities")):
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        seen = {
            str(a.get("name") or "").strip()
            for a in _list_field(ent.get("attributes"))
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        }
        if len(seen) > MAX_ATTRIBUTES_PER_ENTITY:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.attribute_over_limit",
                params={"name": name, "count": len(seen),
                        "max": MAX_ATTRIBUTES_PER_ENTITY},
                fallback=f"实体「{name}」的属性数 {len(seen)} 超出上限 "
                         f"{MAX_ATTRIBUTES_PER_ENTITY}——继续保存会丢弃超出部分，"
                         "已拒绝（请先删除多余属性）",
            ))


#: 属性落点里可跨层继承的语义槽（P1-5 mapping）。
#: **`role` 故意不在其中**：它的既有语义是「入参省略 = 清空」，若补回，
#: 旧客户端就再也删不掉人工填的角色。
_LANDING_CARRY_SLOTS = ("filter", "aggregation", "grain", "enabled")


def _landing_slot_value(landing: dict, slot: str):
    """落点单槽的归一值；`None` = 该键**不落**。

    - `filter` / `aggregation` / `grain`：strip 后非空才落，超 `MAX_LANDING_SLOT_CHARS`
      截断（与 `concept_expansion._unique_attribute_sources` 同一口径）；
    - `enabled`：布尔化，只有显式 `False` 是停用（缺键调用方不应走到这里）。
    """
    if slot == "enabled":
        return landing.get("enabled") is not False
    value = _clean_str(landing.get(slot))
    return value[:MAX_LANDING_SLOT_CHARS] if value else None


def _landing_index_key(entity_name: str, attribute_name: str, landing: dict) -> tuple:
    """落点身份键：`(实体, 属性, ds, entity_name, attribute_name)`，名字两侧 casefold。"""
    return (
        entity_name.casefold(),
        attribute_name.casefold(),
        _clean_str(landing.get("data_source_id")),
        _clean_str(landing.get("entity_name")).casefold(),
        _clean_str(landing.get("attribute_name")).casefold(),
    )


#: 给「没有描述」的实体补列名时取几个。取值是 token 预算的取舍，理由见 `_source_entities`。
_ENTITY_COLUMN_SAMPLE = 8

# 聚焦提示词限长（畸形请求闸门；正常用户输入远短于此）
_FOCUS_PROMPT_MAX_CHARS = 500


def _sample_columns(entity: dict) -> list[str]:
    """从 `attributes[].name` 取前 `_ENTITY_COLUMN_SAMPLE` 个列名。

    **只取名字、不取类型**：这一层要的是「这张表在说什么」的线索（`companyid` 比
    `varchar(64)` 有用得多），而类型对「是不是同一个概念」没有增量信息。

    容忍畸形输入（缺 `attributes` / 不是列表 / 混着 null 与空名）—— 这是喂给模型的
    输入层，一个坏实体不该毁掉整次生成。
    """
    attrs = entity.get("attributes")
    if not isinstance(attrs, list):
        return []
    out: list[str] = []
    for a in attrs:
        if not isinstance(a, dict):
            continue
        col = str(a.get("name") or "").strip()
        if col:
            out.append(col)
        if len(out) >= _ENTITY_COLUMN_SAMPLE:
            break
    return out


class AbstractSchemaService:
    """抽象 schema 的生成 / 读取 / 保存。无状态，每次调用自己开 session。"""

    # ── 生成 ──────────────────────────────────────────────────────

    async def _propose_entities(self, tenant_id: str, kb_id: str, user_id: str,
                                source_entities: list[dict]) -> tuple[list[dict], int]:
        """LLM 聚类 → 过滤悬空映射 → v1→v2 实体列表。`generate` 与 `construct`
        空行兜底共用（**行为与抽取前逐字相同**，含异常与日志口径）。"""
        try:
            raw_concepts = await propose_concept_clusters(
                source_entities, tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("抽象概念聚类失败: kb=%s err=%s", kb_id, exc)
            raise UpstreamServiceError(
                message=translate(
                    "err.abstract_schema.llm_failed",
                    params={"err": str(exc)[:200]},
                    fallback=f"概念聚类失败（{str(exc)[:200]}）",
                ),
                cause=exc if isinstance(exc, Exception) else None,
            ) from exc

        allowed = {(e["data_source_id"], e["entity_name"]) for e in source_entities}
        kept = filter_dangling_sources(raw_concepts, allowed)
        dropped = _count_sources(raw_concepts) - _count_sources(kept)
        concepts = normalize_concepts(kept)
        if not concepts:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.empty_concepts",
                fallback="概念提取没有产出任何可用的概念簇，"
                         "请检查各数据源的 schema 描述是否充分",
            ))
        # LLM 仍产概念簇；落库一律 v2（概念 → 实体，落点补 included=true）。
        # v2 的完整构造（属性/关系）属「自动构造」（construct），这里只做形状搬运。
        schema = migrate_v1_concepts(concepts)
        return schema["entities"], max(dropped, 0)

    async def preview_focus(self, tenant_id: str, kb_id: str, prompt: str,
                            user_id: str = "") -> dict:
        """聚焦生成·解析预览：提示词 → 种子 + 1 跳关联（**不落库、不占生成守卫**）。

        ⚠️ 失败一律抛，绝不「静默回退全量」：用户点了聚焦就是想聚焦，悄悄生成
        全量等于把他不要的表全放出来 —— 比报错更难发现。

        返回 `{"seeds": [{data_source_id, entity_name, source_name, description,
        role, concept}], "neighbors": [{data_source_id, entity_name, source_name,
        via: [...]}], "warnings": [str]}`。
        """
        tenant_id = require_tenant(tenant_id)
        text = (prompt or "").strip()
        if not text:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.focus_prompt_required",
                fallback="请输入聚焦提示词（要生成全量请直接点「全量生成」）",
            ))
        if len(text) > _FOCUS_PROMPT_MAX_CHARS:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.focus_prompt_too_long",
                params={"max": _FOCUS_PROMPT_MAX_CHARS},
                fallback=f"聚焦提示词过长（最多 {_FOCUS_PROMPT_MAX_CHARS} 字）",
            ))

        rows, ds_names = await self._load_source_schemas(tenant_id, kb_id)
        source_entities = self._source_entities(rows, ds_names)
        if not source_entities:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.no_source_schema",
                fallback="该知识库下没有任何已生成 schema 的数据源，无法提取抽象概念",
            ))

        try:
            groups = await resolve_focus_entities(
                text, source_entities,
                tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("聚焦解析失败: kb=%s err=%s", kb_id, exc)
            raise UpstreamServiceError(
                message=translate(
                    "err.abstract_schema.focus_llm_failed",
                    params={"err": str(exc)[:200]},
                    fallback=f"聚焦解析失败（{str(exc)[:200]}）",
                ),
                cause=exc if isinstance(exc, Exception) else None,
            ) from exc

        allowed = {(e["data_source_id"], e["entity_name"]) for e in source_entities}
        seeds, dropped = validate_focus_seeds(groups, allowed)
        if not seeds:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.focus_empty",
                fallback="没有解析出与提示词相关的实体，请调整提示词",
            ))
        logger.info("聚焦解析: kb=%s 种子=%d 剔除成员=%d", kb_id, len(seeds), dropped)

        graph = build_relation_graph(rows)
        _included, neighbors = expand_focus_closure(seeds, graph)

        by_pair = {(e["data_source_id"], e["entity_name"]): e for e in source_entities}
        seed_items = []
        for s in seeds:
            src = by_pair.get((s["data_source_id"], s["entity_name"]), {})
            seed_items.append({**s, "source_name": src.get("source_name", ""),
                               "description": src.get("description", "")})
        neighbor_items = []
        for n in neighbors:
            src = by_pair.get((n["data_source_id"], n["entity_name"]), {})
            neighbor_items.append({**n, "source_name": src.get("source_name", "")})

        warnings: list[str] = []
        if len(seeds) > FOCUS_SEED_SOFT_WARN:
            # fallback 保留 {count} 占位：translate 对 fallback 同样走 str.format
            # （i18n.translate 渲染链的末级），插值后与旧文案逐字相同。
            warnings.append(translate(
                "err.abstract_schema.focus_seeds_soft_warn",
                params={"count": len(seeds)},
                fallback="解析出 {count} 个重点实体，数量较多，生成可能较慢",
            ))
        return {"seeds": seed_items, "neighbors": neighbor_items, "warnings": warnings}

    async def generate(
        self, tenant_id: str, kb_id: str, user_id: str = "", force: bool = False,
        focus_seeds: Optional[list] = None,
        # 缺省仍按「只从数据源生成」——文档本体种子是显式可选项（前端总会带 seed）。
        # 这样旧的 generate 调用方（脚本/测试/老客户端）行为不变。
        mode: str = "replace", seed: str = "structured",
        auto_landings: bool = False, llm_names: Optional[bool] = None,
        alias_hints: str = "",
    ) -> dict:
        """重新生成候选（统一入口，内部走 `construct` 的完整管线）。

        2026-10-19 起不再维护「只产实体、不产关系」的旧实现：实体、属性、关系、
        可选 LLM 落点都由同一条构造管线产出，避免两个入口行为漂移。
        `mode="replace"` = 全部重新构造；`mode="append"` = 只增不删追加。
        聚焦路径由 `focus_seeds` 触发（此时忽略文档种子，只处理聚焦闭包内的源实体）。
        """
        return await self.construct(
            tenant_id, kb_id, user_id=user_id, force=force,
            llm_names=llm_names, seed=seed, mode=mode,
            auto_landings=auto_landings, focus_seeds=focus_seeds,
            alias_hints=alias_hints,
        )

    # ── 自动构造（补齐式）─────────────────────────────────────────

    async def construct(self, tenant_id: str, kb_id: str, user_id: str = "",
                        force: bool = False, llm_names: Optional[bool] = None,
                        seed: str = "structured",
                        auto_landings: bool = False,
                        mode: str = "append",
                        focus_seeds: Optional[list] = None,
                        alias_hints: str = "") -> dict:
        """补齐式构造：源结构 + 证据 → 完整草稿（`proposed`），返回构造摘要。

        `seed="document"` 时先以**文档本体**（KB 级 compiled schema）为种子，
        再用结构化数据源做只增不删的补齐；默认 `"structured"` 保持旧行为。

        语义与错误口径见 spec `2026-10-03-abstract-schema-agent-construct-design.md`：
        - **失败不落库、不覆盖**（全流程内存，只在最后写一次）；
        - 已有 `active` 且非 `force` → `ResourceConflictError`（前端据此二次确认）；
        - 同一 (租户, 知识库) 同时只许跑一次（与 `generate` **共享同一把守卫**）：
          在飞 → **429**（不得用 1003/409，理由见 `generate` 的注释）。
        """
        tenant_id = require_tenant(tenant_id)
        if seed not in ("structured", "document"):
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.invalid_seed",
                fallback="seed 只能是 structured 或 document",
            ))
        if not isinstance(auto_landings, bool):
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.invalid_auto_landings",
                fallback="auto_landings 必须是布尔值",
            ))
        if mode not in ("replace", "append"):
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.invalid_mode",
                fallback="mode 只能是 replace 或 append",
            ))
        if focus_seeds is not None and not isinstance(focus_seeds, list):
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.focus_seeds_bad",
                fallback="focus_seeds 必须是数组",
            ))
        if isinstance(focus_seeds, list) and len(focus_seeds) > FOCUS_SEEDS_MAX:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.focus_seeds_too_many",
                params={"max": FOCUS_SEEDS_MAX},
                fallback=f"focus_seeds 过多（最多 {FOCUS_SEEDS_MAX} 条）",
            ))
        async with get_db_session() as session:
            existing = await OntologyAbstractSchemaRepository(session).get_by_kb(
                tenant_id, kb_id,
            )
        if existing is not None and (existing.status or "") == "active" and not force:
            raise ResourceConflictError(
                message=translate(
                    "err.abstract_schema.active_exists",
                    fallback="该知识库已有已确认的抽象 schema，重新生成会把它退回未确认状态；"
                             "确认覆盖请带 force=true 重试",
                ),
                details={"schema_version": existing.schema_version},
            )
        acquired, guard = await _acquire_generate_guard(tenant_id, kb_id)
        if not acquired:
            raise RateLimitExceededError(
                message=translate(
                    "err.abstract_schema.construct_in_flight",
                    fallback="该知识库正在生成/构造中，请稍后再试",
                ),
                details={"knowledge_base_id": kb_id},
            )
        try:
            return await self._construct_locked(
                tenant_id, kb_id, user_id, existing,
                llm_names=llm_names, seed=seed, auto_landings=auto_landings,
                mode=mode, focus_seeds=focus_seeds, alias_hints=alias_hints,
            )
        finally:
            if guard is not None:
                await _release_generate_guard(guard)

    def _make_probe(self, tenant_id: str, warnings: list[str], *,
                    budget_s: float = 0.0):
        """定向探针闭包：`(ds_id, table, column) -> list[str] | None`。

        - `query_config` 每源只取一次（未登记只读凭据 / 非库源 → None，该源候选记不可测），
          取不到时**补一条 warning**（每源至多一条；异常路径只记类名，绝不带原文）；
        - 单列失败 → 返回 None（不可测），首几条进 warnings；
        - 单列探测超时**钳到阶段预算的剩余部分**：只挡「起新探」的话，在飞的那一发
          会按默认 60s 跑满，探针阶段能超到预算 + 60s；钳制后阶段上界 ≈ `budget_s` + ε
          （下钳 1s，保证预算尾部起的那一发仍有最小机会）。
        - `DataSourceService` 惰性 import：这是运行时依赖，不进模块 import 图（防环）。
        """
        from .data_source_service import DataSourceService

        ds_service = DataSourceService()
        resolved: dict[str, object] = {}
        cache: dict[tuple, Optional[list]] = {}
        started = time.monotonic()

        async def probe(ds_id: str, table: str, column: str) -> Optional[list]:
            key = (ds_id, table, column)
            if key in cache:
                return cache[key]
            if ds_id not in resolved:
                failed_name: Optional[str] = None
                try:
                    resolved[ds_id] = await ds_service.query_config(tenant_id, ds_id)
                except Exception as exc:  # noqa: BLE001 —— 探不动 = 该源相关候选全部不可测
                    resolved[ds_id] = None
                    failed_name = type(exc).__name__
                if resolved[ds_id] is None and len(warnings) < 5:
                    warnings.append(
                        f"数据源 {ds_id} 探针不可用（{failed_name or '未登记只读凭据'}），"
                        "其候选记不可测"
                    )
            pair = resolved[ds_id]
            values: Optional[list] = None
            if pair is not None:
                source, cfg = pair  # type: ignore[misc]
                try:
                    values = await probe_distinct(
                        source, cfg, table, column,
                        timeout_s=max(1.0, budget_s - (time.monotonic() - started)),
                    )
                except Exception as exc:  # noqa: BLE001
                    if len(warnings) < 5:
                        warnings.append(
                            f"补边探针失败 {table}.{column}: {type(exc).__name__}",
                        )
            cache[key] = values
            return values

        return probe

    async def _construct_locked(self, tenant_id: str, kb_id: str, user_id: str,
                                existing, *, llm_names: Optional[bool],
                                seed: str = "structured",
                                auto_landings: bool = False,
                                mode: str = "append",
                                focus_seeds: Optional[list] = None,
                                alias_hints: str = "") -> dict:
        """守卫已在手的构造主体（读取 → 补齐 → 名字档 → 一次 upsert → 摘要）。"""
        rows, ds_names = await self._load_source_schemas(tenant_id, kb_id)
        all_source_entities = self._source_entities(rows, ds_names)
        source_entities = all_source_entities
        focus_meta = None
        if focus_seeds:
            allowed = {
                (e["data_source_id"], e["entity_name"]) for e in all_source_entities
            }
            alive, dropped_seeds = filter_focus_seeds(focus_seeds, allowed)
            if not alive:
                raise InvalidParameterError(message=translate(
                    "err.abstract_schema.focus_seeds_stale",
                    fallback="提交的重点实体在数据源里已全部不存在，请重新解析",
                ))
            graph = build_relation_graph(rows)
            included, _neighbors = expand_focus_closure(alive, graph)
            source_entities = [
                e for e in all_source_entities
                if (e["data_source_id"], e["entity_name"]) in included
            ]
            n_partner = sum(1 for s in alive if s.get("role") == "partner")
            focus_meta = {
                "seeds": len(alive) - n_partner,
                "partners": n_partner,
                "neighbors": len(included) - len(alive),
                "included_tables": len(included),
                "excluded_tables": len(all_source_entities) - len(included),
                "dropped_seeds": dropped_seeds,
            }
            logger.info(
                "抽象 schema 聚焦构造: kb=%s 种子=%d（剔除 %d）纳入=%d 排除=%d",
                kb_id, len(alive), dropped_seeds, len(included),
                len(all_source_entities) - len(included),
            )
        bundles = build_source_bundles(rows, ds_names)

        # getattr 而不是 existing.schema：force 覆盖的旧行可能是迁移窗口里的
        # 部分形状（测试桩也可能只带 status/version），缺 schema 按空基线处理。
        _raw_base = getattr(existing, "schema", None) if existing is not None else None
        base = _raw_base if isinstance(_raw_base, dict) else {}
        if mode == "replace":
            base_entities = []
            base_relations = []
            # 指标是人工登记物，重构造不清理（与旧 generate 同口径）。
            base_metrics = _list_field(base.get("metrics"))
        else:
            base_entities = _list_field(base.get("entities"))
            base_relations = _list_field(base.get("relations"))
            base_metrics = _list_field(base.get("metrics"))
        warnings: list[str] = []

        # 聚焦路径只处理选中的源实体，不再叠加整份文档种子（否则聚焦会被
        # 文档实体旁路）。seed=document 但带 focus_seeds 时按结构化路径处理。
        if seed == "document" and not focus_seeds:
            doc_entities, doc_relations, doc_warnings = await self._load_document_seed(
                tenant_id, kb_id,
            )
            warnings.extend(doc_warnings)
            if doc_entities:
                # 先按名称/别名挂上同名数据源落点，后面的属性补齐与关系提升才有依据。
                attach_landings_by_name(doc_entities, bundles)
            # 只增不删：已有草稿在前（先到者胜），文档种子追加；同名由 normalize 合并。
            base_entities = [*base_entities, *doc_entities]
            base_relations = [*base_relations, *doc_relations]

        # 可选的 LLM 自动落点：只用于「复用文档本体」模式；失败 fail-open
        # （保留确定性落点并回 warning），不阻断构造。
        landing_stats: dict = {"entities": 0, "attributes": 0, "error": None}
        dropped_total = 0
        if auto_landings and seed == "document" and not focus_seeds and bundles and base_entities:
            source_catalog, catalog_warnings = self._source_landing_catalog(bundles)
            warnings.extend(catalog_warnings)
            if source_catalog:
                try:
                    alignments = await align_document_landings(
                        self._document_landing_catalog(base_entities),
                        source_catalog,
                        tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                    )
                    landing_stats.update(
                        apply_llm_landings(base_entities, alignments, bundles)
                    )
                except Exception as exc:  # noqa: BLE001 —— 对齐是增益，不拖垮构造
                    logger.error("LLM 自动落点对齐失败: kb=%s err=%s", kb_id, exc)
                    landing_stats["error"] = str(exc)[:200]
                    warnings.append(
                        f"自动落点对齐失败（已保留确定性落点）：{str(exc)[:200]}"
                    )

        if not base_entities and not source_entities:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.no_source_schema",
                fallback="该知识库既没有可复用的文档本体 schema，"
                         "也没有已生成 schema 的数据源，无法自动构造",
            ))

        if not base_entities:
            # 空行兜底：没有对齐先聚类（与 generate 同一条产线），再照常补齐。
            base_entities, dropped = await self._propose_entities(
                tenant_id, kb_id, user_id, source_entities,
            )
            dropped_total += max(dropped, 0)
            if dropped:
                warnings.append(f"对齐兜底剔除 {dropped} 条无效源映射")
        elif source_entities:
            # 结构化侧只做「补齐」：聚类产物追加在既有实体/文档种子之后，同名由
            # normalize 合并。与 seed 无关 —— 追加模式下也要能发现新实体，
            # 否则「追加」退化成只补属性/关系。
            # （条件不能只写 seed=="document"：那样 append + structured 永远不会聚类。）
            doc_end = len(base_entities)   # 文档种子/既有草稿的边界（聚类从这之后开始）
            structured_entities, dropped = await self._propose_entities(
                tenant_id, kb_id, user_id, source_entities,
            )
            base_entities = [*base_entities, *structured_entities]
            dropped_total += max(dropped, 0)
            if dropped:
                warnings.append(f"结构化补齐剔除 {dropped} 条无效源映射")
            # 文档种子与聚类常把同一张表建成两个实体（名字不同、落点相同）。
            # 只把「聚类 → 既有实体」合并，文档种子之间同源不合并（可能是刻意概念）。
            base_entities, merged_dupes = merge_duplicate_source_entities(
                base_entities, doc_end,
            )
            if merged_dupes:
                warnings.append(f"已合并 {merged_dupes} 个与文档实体同源的重复聚类实体")

        # 业务词表：给实体挂别名；目标在数据源 schema 里但抽象层没有 → 自动补建实体。
        # 必须在 `filled` 之前 —— 新建实体也要按落点补齐属性。
        if alias_hints:
            hinted, created, hint_warnings = apply_alias_hints_to_entities(
                base_entities, alias_hints, bundles,
            )
            warnings.extend(hint_warnings)
            if created:
                warnings.append(f"业务词表补建了 {created} 个实体")
            if hinted or created:
                logger.info("抽象 schema 业务词表：挂别名 %d 个实体 / 补建 %d 个", hinted, created)
            # 指标同表登记：`[metric] 名称 | 别名 | 目标实体 | 描述` → schema.metrics。
            # 指标不是从文档/库结构自动生成的（人登记的口径）；写进词表才能在重建后
            # 一次生成出来，不用再手工进指标面板补。
            metric_hints, metric_warnings = parse_metric_hints(alias_hints, bundles)
            warnings.extend(metric_warnings)
            if metric_hints:
                existing_names = {
                    str(m.get("name") or "") for m in base_metrics if isinstance(m, dict)
                }
                added = 0
                for metric in metric_hints:
                    if metric["name"] not in existing_names:
                        base_metrics.append(metric)
                        existing_names.add(metric["name"])
                        added += 1
                if added:
                    logger.info("抽象 schema 业务词表：登记 %d 个指标", added)

        landing_map = build_landing_map(base_entities)

        filled: list[dict] = []
        for ent in base_entities:
            if not isinstance(ent, dict) or not str(ent.get("name") or "").strip():
                continue
            merged = merged_attributes_for_entity(ent, bundles)
            if merged:
                filled.append({"name": ent["name"], "attributes": merged})

        lifted, lift_stats = lifted_relations(bundles, landing_map, reserved=base_relations)

        candidates = cross_source_candidates(base_entities, base_relations, bundles, landing_map)
        probes = self._make_probe(
            tenant_id, warnings,
            budget_s=float(config.ABSTRACT_CONSTRUCT_PROBE_BUDGET_S),
        )
        scored, edge_stats = await collect_edge_scores(
            candidates, probes,
            budget_s=float(config.ABSTRACT_CONSTRUCT_PROBE_BUDGET_S),
            max_queries=int(config.ABSTRACT_CONSTRUCT_MAX_PROBE_QUERIES),
        )
        existing_rel_names = {
            str(r.get("name") or "") for r in base_relations if isinstance(r, dict)
        }
        suggestions: list[dict] = []
        for scan in scored:
            suggestion = build_edge_suggestion(
                scan, scan["left_score"], scan["right_score"],
            )
            if suggestion is None or suggestion["name"] in existing_rel_names:
                continue
            suggestions.append(suggestion)

        # 「只增不删」靠这一行的顺序：base 在前、raw 在后（同名合并先到者胜）。
        schema = normalize_schema({
            "entities": [*base_entities, *filled],
            "relations": [*base_relations, *lifted, *suggestions],
            "metrics": [*base_metrics],
            # `tools` 覆盖块是人工登记物，构造管线不产它（= 不管理）：从旧行原样
            # 并入（与公开保存的保底同语义）。construct **直写 upsert、不经 save**，
            # 不在这里带上的话，有覆盖的 KB 点一次「自动构造」覆盖就静默消失。
            # 旧行无覆盖时归一化后不留键（与既有三键/四键形状一致）。
            "tools": _list_field(base.get("tools")),
        })
        # normalize 的条数上限是静默的：截断在这里显形（spec §四/§五「上限截断 + warning」）。
        _append_cap_warnings(
            warnings, schema,
            [*base_entities, *filled],
            [*base_relations, *lifted, *suggestions],
            [*base_metrics],
        )

        names_enabled = (
            bool(config.ABSTRACT_CONSTRUCT_NAMES_ENABLED) if llm_names is None else llm_names
        )
        names_stats: dict = {"patched": 0, "rejected": 0, "batches_done": 0,
                             "batches_skipped": 0, "error": None, "enabled": False}
        if names_enabled:
            names_stats = await run_name_stage(
                schema["entities"],
                lambda batch: suggest_attribute_display_names(
                    batch, tenant_id=tenant_id, kb_id=kb_id, user_id=user_id,
                ),
                budget_s=float(config.ABSTRACT_CONSTRUCT_NAMES_BUDGET_S),
                concurrency=int(config.ABSTRACT_CONSTRUCT_NAMES_CONCURRENCY),
                call_timeout_s=float(config.ABSTRACT_CONSTRUCT_NAMES_CALL_TIMEOUT_S),
            )
            names_stats["enabled"] = True
            if names_stats.get("error"):
                warnings.append(f"中文名生成部分失败（确定性部分已保留）：{names_stats['error']}")
            if names_stats.get("batches_skipped"):
                warnings.append(
                    f"中文名预算用完，{names_stats['batches_skipped']} 批未跑（再点一次「自动构造」可续跑）"
                )

        attrs_added = max(0, _count_attrs(schema["entities"]) - _count_attrs(base_entities))
        remaining = sum(
            1
            for ent in schema["entities"] if isinstance(ent, dict)
            for attr in _list_field(ent.get("attributes"))
            if isinstance(attr, dict) and needs_display_name(attr)
        )

        async with get_db_session() as session:
            row = await OntologyAbstractSchemaRepository(session).upsert(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                schema=schema,
                status="proposed",
                # 与 generate 同路：构造是「从源生成」，source_hash 描述
                # 「这份 schema 是对着哪一版源做的」。
                source_hash=_source_hash(all_source_entities),
                edited_by=user_id or None,
                # CAS = 构造开始时读到的那一版（期间真被别人改过才判 1003）。
                expected_version=(
                    existing.schema_version if existing is not None else None
                ),
            )

        logger.info(
            "抽象 schema 自动构造: kb=%s entities=%d attrs+=%d rels+=%d sug=%d "
            "lifted=%d unmapped=%d names=%d/%d probes=%d skipped=%d v=%s by=%s",
            kb_id, len(schema["entities"]), attrs_added,
            max(0, len(schema["relations"]) - len(base_relations)),
            len(suggestions), lift_stats["lifted"], lift_stats["unmapped"],
            names_stats.get("patched", 0), remaining,
            edge_stats["queries"], edge_stats["skipped"],
            row.schema_version, user_id or "-",
        )
        result = row.to_dict()
        result.update({
            "entities": len(schema["entities"]),
            "attributes": {"added": attrs_added},
            "relations": {
                "added": max(0, len(schema["relations"]) - len(base_relations)),
                "lifted": lift_stats["lifted"],
                "unmapped": lift_stats["unmapped"],
                "renamed": lift_stats["renamed"],
            },
            "suggestions": {
                "added": len(suggestions),
                "candidates": len(candidates),
                "untestable": edge_stats["skipped"],
                "failed": edge_stats["failed"],
            },
            "names": {
                "enabled": bool(names_stats.get("enabled")),
                "patched": names_stats.get("patched", 0),
                "rejected": names_stats.get("rejected", 0),
                "remaining": remaining,
                "batches_skipped": names_stats.get("batches_skipped", 0),
            },
            "probes": {"queries": edge_stats["queries"], "skipped": edge_stats["skipped"]},
            "landings": landing_stats,
            "dropped_sources": max(dropped_total, 0),
            "stale_count": 0,
            "warnings": warnings,
        })
        if focus_meta is not None:
            result["focus"] = focus_meta
        return result

    # ── 读取（含读时标脏）─────────────────────────────────────────

    async def get(self, tenant_id: str, kb_id: str) -> Optional[dict]:
        """读出对齐；**标脏在这里现算**（决策 7）。

        标脏是派生量、**不落库**，理由是它的检测时机：写路径有四条（画布编辑器
        保存 / generate-schema / YAML 导入 / 模板 reseed），逐条挂钩子漏一个就是
        一次静默的「标脏失效」；而读时算没有「漏挂」这个失败模式 —— 它只看
        **当下**的源 schema，谁改的、什么时候改的都无所谓。

        代价是每次读要拉一遍该库的 compiled schema。这是配置页的低频读，
        可接受；检索侧不走这里（那边只认 `status='active'`，不关心标脏）。

        没有任何对齐时返回 `None`（前端据此显示空态），而不是一个空壳对象。

        行内容按 v2 读出（`schema: {entities, relations}`）；迁移窗口内若读到 v1
        遗留行（列里还是概念数组），按迁移结果现算返回，读一次都不会写回去。
        """
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            row = await OntologyAbstractSchemaRepository(session).get_by_kb(
                tenant_id, kb_id,
            )
            if row is None:
                return None
            rows = await OntologySchemaRepository(session).list_compiled_schemas(
                tenant_id, kb_id,
            )
            ds_rows = await KnowledgeDataSourceRepository(session).list_by_kb(
                tenant_id, kb_id, offset=0, limit=500,
            )

        declared = self._declared_index(rows)
        live_sources = {d.id for d in ds_rows}
        ds_names = {d.id: d.name for d in ds_rows}
        payload = row.to_dict()
        raw = payload.get("schema")
        if isinstance(raw, list):
            # 过渡兼容（迁移窗口）：v1 行（概念数组）按迁移结果返回。
            # 迁移脚本执行后本分支恒不命中；保留一个版本周期以便回滚读。
            raw = migrate_v1_concepts(normalize_concepts(raw))
        annotated, stale_count = self._annotate_schema_staleness(
            raw if isinstance(raw, dict) else {"entities": [], "relations": []},
            declared, live_sources, ds_names,
        )
        base_metrics = _list_field(raw.get("metrics")) if isinstance(raw, dict) else []
        # `tools` 是**可选**覆盖块：只有非空时才并入 schema，空/缺一律不留键
        # （无覆盖的行读出来的形状与今天逐字节相同，快照指纹也不变）。
        base_tools = _list_field(raw.get("tools")) if isinstance(raw, dict) else []
        payload["schema"] = {**annotated, "metrics": base_metrics}
        if base_tools:
            payload["schema"]["tools"] = base_tools
        payload["stale_count"] = stale_count
        return payload

    # ── 保存 / 确认 ───────────────────────────────────────────────

    @staticmethod
    def _carry_landing_slots_from_row(incoming: dict, stored: dict | None) -> dict:
        """入参落点**缺槽键**时，从已存行继承（P1-5 跨层防护）。

        旧客户端（画布 / YAML 导入 / 模板 reseed）重建属性落点时只带三元组，
        `normalize_schema` 会把缺失的可选槽当成「没有」——一次与 mapping 无关
        的保存就静默清掉人工登记的 `filter` / `aggregation` / `grain` / `enabled`。
        这里在归一化**之前**按落点身份键从当前行补回：

        - 入参**带键**即入参赢：`filter=""`（显式空串）表示清空，不继承、且
          输出不留空键（与归一化「非空才落」同形状）；
        - 入参缺键且已存落点带该键 → 继承其归一值；
        - 新落点 / 已存行缺该键 / 没有已存行 → 不落键（与今天逐字节相同）；
        - **`role` 不在继承范围**（既有语义是「省略 = 清空」）。

        纯函数：不读库、不改入参，返回新的 dict/list 树（未触及的子树按引用复用，
        不会被本函数写）。只处理**属性级**落点；实体级落点（`entities[].sources`）
        不在此列。
        """
        if not isinstance(incoming, dict):
            return incoming
        entities = incoming.get("entities")
        if not isinstance(entities, list):
            return {**incoming}

        stored_by_key: dict[tuple, dict] = {}
        if isinstance(stored, dict):
            for entity in _list_field(stored.get("entities")):
                if not isinstance(entity, dict):
                    continue
                ent_name = _clean_str(entity.get("name"))
                if not ent_name:
                    continue
                for attr in _list_field(entity.get("attributes")):
                    if not isinstance(attr, dict):
                        continue
                    attr_name = _clean_str(attr.get("name"))
                    if not attr_name:
                        continue
                    for landing in _list_field(attr.get("sources")):
                        if isinstance(landing, dict):
                            stored_by_key.setdefault(
                                _landing_index_key(ent_name, attr_name, landing),
                                landing,
                            )

        out_entities: list = []
        for entity in entities:
            if not isinstance(entity, dict):
                out_entities.append(entity)
                continue
            ent_name = _clean_str(entity.get("name"))
            new_entity = dict(entity)
            attrs = entity.get("attributes")
            if isinstance(attrs, list):
                new_attrs: list = []
                for attr in attrs:
                    if not isinstance(attr, dict):
                        new_attrs.append(attr)
                        continue
                    new_attr = dict(attr)
                    attr_name = _clean_str(attr.get("name"))
                    sources = attr.get("sources")
                    if isinstance(sources, list):
                        new_sources: list = []
                        for landing in sources:
                            if not isinstance(landing, dict) or not (ent_name and attr_name):
                                new_sources.append(landing)
                                continue
                            old = stored_by_key.get(
                                _landing_index_key(ent_name, attr_name, landing))
                            merged = dict(landing)
                            for slot in _LANDING_CARRY_SLOTS:
                                if slot in landing:
                                    value = _landing_slot_value(landing, slot)
                                    if value is None:
                                        merged.pop(slot, None)
                                    else:
                                        merged[slot] = value
                                elif isinstance(old, dict) and slot in old:
                                    value = _landing_slot_value(old, slot)
                                    if value is not None:
                                        merged[slot] = value
                            new_sources.append(merged)
                        new_attr["sources"] = new_sources
                    new_attrs.append(new_attr)
                new_entity["attributes"] = new_attrs
            out_entities.append(new_entity)

        return {**incoming, "entities": out_entities}

    @staticmethod
    def _carry_metric_executable_from_row(incoming: dict, stored: dict | None) -> dict:
        """入参**缺** `executable` 键时，按指标名从已存行继承（W3.3 M2）。

        旧客户端（画布 / YAML 导入 / 模板 reseed）重建 metrics 条目时不认识
        `executable`，一次与指标无关的保存会把它静默清掉 —— 这里在归一化之前
        按 `name` 从当前行补回，与落点语义槽继承（`_carry_landing_slots_from_row`）
        同一纪律：

        - 入参**带键即入参赢**：`null` = 显式清空，不继承、归一化后不留键；
        - 入参缺键且已存同名条目带 dict → 继承其值（深拷贝，不与库行共享）；
        - 新指标 / 已存行没有该键 / 没有已存行 → 不落键（与今天逐字节相同）；
        - **权威内部写**（`_carry_landing_slots=False`：版本恢复 / 演化 apply）
          不调用本函数 —— 恢复旧快照时不能把当前行的定义注回去，否则重算
          hash 与快照 hash 不等，恢复恒失败。

        纯函数：不读库、不改入参；只重建 `metrics` 列表。
        """
        if not isinstance(incoming, dict):
            return incoming
        metrics = incoming.get("metrics")
        if not isinstance(metrics, list):
            return {**incoming}
        stored_by_name: dict[str, dict] = {}
        if isinstance(stored, dict):
            for metric in _list_field(stored.get("metrics")):
                if not isinstance(metric, dict):
                    continue
                name = _clean_str(metric.get("name"))
                executable = metric.get("executable")
                if name and isinstance(executable, dict):
                    stored_by_name.setdefault(name, executable)
        out: list = []
        for metric in metrics:
            if not isinstance(metric, dict):
                out.append(metric)
                continue
            new_metric = dict(metric)
            if "executable" not in metric:
                name = _clean_str(metric.get("name"))
                inherited = stored_by_name.get(name) if name else None
                if inherited is not None:
                    new_metric["executable"] = copy.deepcopy(inherited)
            out.append(new_metric)
        return {**incoming, "metrics": out}

    async def _record_snapshot(
        self,
        session,
        tenant_id: str,
        kb_id: str,
        row,
        *,
        source: str = "manual",
        saved_by: Optional[str] = None,
    ) -> None:
        """写一条保存快照并裁剪到最近 `_SNAPSHOT_RETENTION` 条（失败只 warning）。

        调用方保证：① 在 save 的**同一 session / 事务**内调用（与 upsert 一起
        提交）；② 只有公开保存（`_carry_landing_slots=True` 且
        `record_snapshot=True`）才调用 —— 演化 apply / 版本恢复已有
        `semantic_versions`，演化 UI 采纳（adopt-alias / adopt-metric）已有
        `evolution_ledgers`，都不重复记。

        失败隔离：PostgreSQL 里一条语句失败会让**整个事务**进入 aborted 状态，
        单纯 `except` 吞掉异常是假的（主保存随后会崩在别处）。所以写入/裁剪包在
        `session.begin_nested()`（SAVEPOINT）里，失败只回滚保存点，主事务继续
        可用。外层 try 同时兜住极端环境下 `begin_nested` 不可用的情况。
        """
        try:
            async with session.begin_nested():
                tid = require_tenant(tenant_id)
                new_id = uuid.uuid4().hex
                await session.execute(insert(AbstractSchemaSaveHistory).values(
                    id=new_id,
                    tenant_id=tid,
                    knowledge_base_id=kb_id,
                    schema_version=row.schema_version,
                    status=row.status,
                    schema=row.schema,
                    saved_by=saved_by or None,
                    source=source,
                ))
                # 保留最新 N 条：取该 KB **全部** id（行数有界：≤ 21 + 并发少量），
                # 删掉窗口外的所有旧行。不能只 `limit(N+1)` 删「第 N+1 名」——
                # 并发双写各自删同一行时，会永久停在 N+1 条、最旧一条永不淘汰。
                result = await session.execute(
                    select(AbstractSchemaSaveHistory.id)
                    .where(
                        AbstractSchemaSaveHistory.tenant_id == tid,
                        AbstractSchemaSaveHistory.knowledge_base_id == kb_id,
                    )
                    .order_by(
                        AbstractSchemaSaveHistory.created_at.desc(),
                        AbstractSchemaSaveHistory.id.desc(),
                    )
                )
                ordered_ids = [r[0] for r in result.all()]
                # 保护刚写入的这一行：同微秒 / 并发重叠时它可能排进窗口外，
                # 按 id 排除，绝不把新鲜快照当旧行裁掉。
                stale_ids = [
                    snapshot_id
                    for snapshot_id in ordered_ids[_SNAPSHOT_RETENTION:]
                    if snapshot_id != new_id
                ]
                if stale_ids:
                    await session.execute(
                        delete(AbstractSchemaSaveHistory).where(
                            AbstractSchemaSaveHistory.id.in_(stale_ids),
                        )
                    )
        except Exception as exc:  # noqa: BLE001 —— 快照是旁路，绝不失败主保存
            logger.warning(
                "抽象 schema 保存快照写入失败（不影响保存）: kb=%s err=%s",
                kb_id, exc, exc_info=True,
            )

    async def save(
        self,
        tenant_id: str,
        kb_id: str,
        schema: dict,
        status: str = "proposed",
        edited_by: str = "",
        expected_version: Optional[int] = None,
        _carry_landing_slots: bool = True,
        record_snapshot: bool = True,
        snapshot_source: str = "manual",
    ) -> dict:
        """界面的改名 / 合并 / 删源 / 确认都走它（入参是 v2 的 `{entities, relations}`）。

        `status='active'` 就是「确认」—— 确认不是另一个动作，只是同一次保存的
        一个字段（这样「改完顺便确认」不会变成两次请求、两个版本号）。

        `expected_version` **必填**：编辑页可能开着很久，中间别人确认过一版，
        不带版本就是拿旧内容盖新内容。

        `_carry_landing_slots`：默认 True = 公开客户端保存，入参缺失的落点
        语义槽从当前行继承（防旧客户端静默清空）。演化应用 / 版本恢复是**权威
        写**，传 False —— 载荷内容已是本批意图，补键会让旧快照指纹对不上。

        `record_snapshot`：默认 True = 写保存历史快照。演化 UI 的采纳
        （adopt-alias / adopt-metric）传 False —— 它们已有 `evolution_ledgers`
        台账，再写快照会占满每 KB 20 条额度、并在 UI 上被误标成「手动」。
        **与 `_carry_landing_slots` 的关系**：两者是独立的「不产快照」原因 ——
        `_carry_landing_slots=False`（演化 apply / 版本恢复）与
        `record_snapshot=False`（演化 UI 采纳）都**不产**快照；只有两者同时为
        True 才写。

        `snapshot_source`：保存历史快照的来源（`manual` / `restore`）。只在
        `_carry_landing_slots=True` 且 `record_snapshot=True` 时写快照；快照
        失败只 warning，不影响保存。
        """
        tenant_id = require_tenant(tenant_id)
        if status not in ABSTRACT_STATUSES:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.invalid_status",
                params={"status": status, "allowed": "/".join(ABSTRACT_STATUSES)},
                fallback=f"非法 status: {status}（允许 {'/'.join(ABSTRACT_STATUSES)}）",
            ))
        if expected_version is None:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.missing_version",
                fallback="缺少 expected_schema_version：保存必须携带版本以做乐观锁",
            ))

        # 属性上限前置闸（缺口修复）：超限输入会被 normalize 静默截断、UI 只看到
        # 「保存成功但属性没了」——拒绝比静默丢好（与导入器合并预检同档）。
        if isinstance(schema, dict):
            _assert_attribute_caps(schema)

        if isinstance(schema, dict):
            # 读一次当前行，供三件事共用（多一次读是有意的，P1-5）：
            # ① metrics 键缺失时的保底；② tools 键缺失时的保底（仅公开保存）；
            # ③ 落点语义槽的跨层继承。
            async with get_db_session() as session:
                old_row = await OntologyAbstractSchemaRepository(session).get_by_kb(
                    tenant_id, kb_id,
                )
            old_schema = getattr(old_row, "schema", None)
            if _carry_landing_slots:
                # 指标可执行定义先按名字继承（W3.3），再补落点语义槽；两者都是
                # 「旧客户端不认识的键」，同一道 gate：权威内部写不继承。
                schema = self._carry_metric_executable_from_row(schema, old_schema)
                schema = self._carry_landing_slots_from_row(schema, old_schema)
            if "metrics" not in schema:
                # 键缺失 = 不管理 metrics 的调用方（旧前端 / YAML 导入 / 模板 reseed）——
                # 从旧行保底，防整份替换清掉人工登记物（对称 construct 的 base_metrics 保底）。
                # 显式 metrics=[...]（含空列表）= 调用方明确给定，原样尊重（空列表 = 用户清空）。
                base_metrics = _list_field(old_schema.get("metrics")) if isinstance(old_schema, dict) else []
                schema = {**schema, "metrics": [*base_metrics]}
            if _carry_landing_slots and "tools" not in schema:
                # **只对公开保存保底**：键缺失 = 不管理 tools 的调用方（旧前端 /
                # YAML 导入 / 模板 reseed），从旧行继承；显式 `tools: []` = 用户
                # 清空，原样尊重（归一后不留空键）。
                # ⚠️ 权威内部写（`_carry_landing_slots=False`：版本恢复 / 演化应用）
                # 里「省略 tools 键」= **清除覆盖**，绝不复活 —— 否则恢复旧快照
                # 时当前行的 tools 会被注回去，重算 hash 与快照 hash 不等，恢复恒失败。
                base_tools = _list_field(old_schema.get("tools")) if isinstance(old_schema, dict) else []
                if base_tools:
                    schema = {**schema, "tools": [*base_tools]}

        normalized = normalize_schema(schema)
        if not normalized["entities"]:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.empty_schema",
                fallback="抽象 schema 至少需要一个实体",
            ))

        async with get_db_session() as session:
            repo = OntologyAbstractSchemaRepository(session)
            # 保留原 source_hash：这次是人改 schema，不是从源重新生成，
            # 指纹仍描述「这份对齐是对着哪一版源做的」
            old = await repo.get_by_kb(tenant_id, kb_id)
            row = await repo.upsert(
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                schema=normalized,
                status=status,
                source_hash=(old.source_hash if old is not None else None),
                edited_by=edited_by or None,
                expected_version=expected_version,
            )
            # 保存历史是旁路：与 upsert 同事务写入；失败只 warning（内部已吞）。
            # 只在公开保存写 —— 演化 apply / 版本恢复有 semantic_versions，
            # 演化 UI 采纳有 evolution_ledgers（`record_snapshot=False`）。
            if _carry_landing_slots and record_snapshot:
                await self._record_snapshot(
                    session, tenant_id, kb_id, row,
                    source=snapshot_source, saved_by=edited_by or None,
                )

        logger.info(
            "抽象 schema 保存: kb=%s entities=%d status=%s v=%s by=%s",
            kb_id, len(normalized["entities"]), status, row.schema_version, edited_by or "-",
        )
        # P2：确认（active）才把抽象模型镜像回 KB 级 compiled schema。
        # proposed 是草稿，不参与检索，也不该改文档抽取用的那一份。
        # 镜像失败不回滚已提交的抽象行，但把失败如实带回（ok=false）。
        mirror: Optional[dict] = None
        if status == "active":
            mirror = await self._mirror_to_kb_compiled_schema(
                tenant_id, kb_id, normalized, edited_by=edited_by,
            )
        # 回带 stale_count：确认时若还有指向不存在实体的对齐，界面要提醒一句
        # （决策 7 只说「提示」，**不阻断**确认 —— 人有权在知道的情况下继续）
        async with get_db_session() as session:
            rows = await OntologySchemaRepository(session).list_compiled_schemas(
                tenant_id, kb_id,
            )
            ds_rows = await KnowledgeDataSourceRepository(session).list_by_kb(
                tenant_id, kb_id, offset=0, limit=500,
            )
        _, stale_count = self._annotate_schema_staleness(
            normalized,
            self._declared_index(rows),
            {d.id for d in ds_rows},
            {d.id: d.name for d in ds_rows},
        )
        return {
            **row.to_dict(),
            "stale_count": stale_count,
            **({"kb_schema_mirror": mirror} if mirror is not None else {}),
        }

    # ── 保存历史：列表 / 详情 / 还原（spec 2026-10-27 §4）────────────

    @staticmethod
    def _snapshot_meta(row) -> dict:
        """列表项：**不含 schema 内容**（几十个实体的 JSON 列表页不需要）。"""
        return {
            "id": row.id,
            "schema_version": row.schema_version,
            "status": row.status,
            "saved_by": row.saved_by,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def list_snapshots(
        self, tenant_id: str, kb_id: str, limit: int = _SNAPSHOT_LIST_DEFAULT,
    ) -> dict:
        """该 KB 最近 N 条保存快照的元数据，**新→旧**；租户 + KB 双重过滤。

        `total` 是该 KB 的快照总数（不是本页条数）——前端分页/提示需要知道
        「还有没有更旧的」；保留策略把它压在 20 以内，count 很便宜。
        """
        tenant_id = require_tenant(tenant_id)
        # 与详情/还原同一把尺子（strip + 空值 1001）：空间闸门在能力层已按归一后的
        # 值判定，列表这里若仍用原值查询，带前后空格的 KB 会「过得了闸门、查不到行」。
        kb_id = _require_snapshot_kb(kb_id)
        limit_value = _clamp_snapshot_limit(limit)
        async with get_db_session() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(AbstractSchemaSaveHistory)
                .where(
                    AbstractSchemaSaveHistory.tenant_id == tenant_id,
                    AbstractSchemaSaveHistory.knowledge_base_id == kb_id,
                )
            )
            result = await session.execute(
                select(AbstractSchemaSaveHistory)
                .where(
                    AbstractSchemaSaveHistory.tenant_id == tenant_id,
                    AbstractSchemaSaveHistory.knowledge_base_id == kb_id,
                )
                .order_by(
                    AbstractSchemaSaveHistory.created_at.desc(),
                    AbstractSchemaSaveHistory.id.desc(),
                )
                .limit(limit_value)
            )
            rows = result.scalars().all()
        return {
            "items": [self._snapshot_meta(row) for row in rows],
            "total": int(total or 0),
        }

    async def get_snapshot(
        self, tenant_id: str, snapshot_id: str, kb_id: Optional[str] = None,
    ) -> dict:
        """单条快照详情（**含 schema**）；不存在 / 别的租户 / 别的 KB → 1002。

        `kb_id` **必填**（空串/None → 1001，见 `_require_snapshot_kb`）：不传就
        只剩「租户 + 32 位 id」一道口，等于把跨 KB 读取的判据交给 id 不可猜性 ——
        这不是权限。能力层还有 `_require_nonblank` 的前置一层。
        """
        tenant_id = require_tenant(tenant_id)
        kb_id = _require_snapshot_kb(kb_id)
        stmt = select(AbstractSchemaSaveHistory).where(
            AbstractSchemaSaveHistory.id == snapshot_id,
            AbstractSchemaSaveHistory.tenant_id == tenant_id,
            AbstractSchemaSaveHistory.knowledge_base_id == kb_id,
        )
        async with get_db_session() as session:
            row = (await session.execute(stmt)).scalars().first()
        if row is None:
            raise ResourceNotFoundError(message=translate(
                "err.abstract_schema.snapshot_not_found",
                fallback="保存历史快照不存在",
            ))
        return {
            **self._snapshot_meta(row),
            "knowledge_base_id": row.knowledge_base_id,
            "schema": row.schema if isinstance(row.schema, dict) else {},
        }

    async def restore_snapshot(
        self,
        tenant_id: str,
        snapshot_id: str,
        expected_version: Optional[int],
        user_id: str = "",
        kb_id: Optional[str] = None,
    ) -> dict:
        """用快照内容走**公开保存**还原：版本推进、乐观锁与确认语义都不绕过。

        - `status` 取快照里存的状态（快照是 active，还原后仍 active）；
        - `edited_by` = 操作人；`expected_version` = **当前行**版本（前端带）；
        - 还原本身再落一条 `source='restore'` 的新快照（`save` 内已有旁路）。
        版本对不上时沿既有 1003 口径抛出，不在这里吞。

        ⚠️ **已知取舍（有意，不在本任务改）**：走公开保存语义
        （`_carry_landing_slots=True`），快照里**没有的键**会从**当前行**继承 ——
        例如演化台写入的 `tools` 覆盖块（及落点语义槽）在旧快照里可能不存在，
        还原后会保留现值。这样旧快照不会把人工维护的覆盖静默清空；代价是
        「还原」不等于「逐字节回到当时」。权威恢复（逐字节）仍走
        `semantic_versions` / `_carry_landing_slots=False` 的内部写。
        """
        snapshot = await self.get_snapshot(tenant_id, snapshot_id, kb_id=kb_id)
        return await self.save(
            tenant_id,
            snapshot["knowledge_base_id"],
            snapshot["schema"],
            status=snapshot["status"],
            edited_by=user_id,
            expected_version=expected_version,
            snapshot_source="restore",
        )

    async def _mirror_to_kb_compiled_schema(
        self, tenant_id: str, kb_id: str, schema: dict, *, edited_by: str = "",
    ) -> dict:
        """active 抽象 → KB 级 compiled schema 的镜像写（best-effort）。"""
        from .ontology_compiler import OntologyCompiler

        try:
            result = await OntologyCompiler().mirror_from_abstract(
                tenant_id, kb_id, schema, edited_by=edited_by or None,
            )
            return {"ok": True, "schema_version": result.get("schema_version")}
        except Exception as exc:  # noqa: BLE001 — 抽象行已提交，镜像失败不拖垮保存
            logger.exception("抽象 schema 镜像回写 KB 级 compiled schema 失败: kb=%s", kb_id)
            return {"ok": False, "error": str(exc)[:200]}

    # ── 共享本体 TBox 导入（spec 2026-10-13-abstract-tbox-import-landing-design.md）──

    async def import_tbox(
        self, tenant_id: str, kb_id: str, yaml_text: str, *,
        user_id: str = "", expected_version: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict:
        """把 nexonto 规划导出的 TBox YAML 导入为本库的抽象 schema（§四）。

        - YAML 在后端解析/映射（前端只传文本）；上限前置检查在映射层（超限 1001）；
        - **反序合并**（§3.3）：导入方标量优先、existing 落点/别名/backings 并入、
          独有实体保留、零落点实体保留；
        - 写入走既有 `save()`（乐观锁）：**status 保留原值**（首次导入 = proposed）、
          metrics 不动（merged 显式带 existing 的）、source_hash 由 save 保留；
        - `dry_run=True`：只算 diff 不写库（不需要 `expected_version`）；
        - 返回 `{dry_run, summary, warnings, ...}`，正式导入平铺 save 回包字段。
        """
        tenant_id = require_tenant(tenant_id)
        raw = tbox_import.parse_tbox_yaml(yaml_text)
        imported, warnings, ignored = tbox_import.map_tbox_to_schema(raw)
        if not imported["entities"]:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.import_no_entities",
                fallback="YAML 里没有任何可导入的实体（名称缺失或超长的条目已被跳过）",
            ))
        async with get_db_session() as session:
            existing_row = await OntologyAbstractSchemaRepository(session).get_by_kb(
                tenant_id, kb_id,
            )
        existing_schema = (
            existing_row.schema
            if (existing_row is not None and isinstance(existing_row.schema, dict))
            else {}
        )
        merged, merge_warnings = tbox_import.merge_imported(existing_schema, imported)
        summary = {
            **tbox_import.build_diff_summary(existing_schema, imported),
            "ignored_sections": ignored,
        }
        warnings = [*warnings, *merge_warnings]
        if dry_run:
            return {"dry_run": True, "summary": summary, "warnings": warnings}
        if expected_version is None:
            # 复用 save 的既有一课：漏带版本 = 静默盖掉别人。首次导入由前端传 0
            #（upsert 对「行不存在」走 INSERT、不看该值；行已存在时 0 会被 CAS 判 1003）。
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.missing_version",
                fallback="缺少 expected_schema_version：保存必须携带版本以做乐观锁",
            ))
        status = (
            existing_row.status
            if (existing_row is not None and existing_row.status in ABSTRACT_STATUSES)
            else "proposed"
        )
        saved = await self.save(
            tenant_id, kb_id, merged, status=status,
            edited_by=user_id, expected_version=expected_version,
        )
        logger.info(
            "TBox 导入: kb=%s entities=%d(新增 %d/更新 %d) attrs+=%d ignored=%s v=%s by=%s",
            kb_id, len(merged["entities"]), summary["entities_added"],
            summary["entities_updated"], summary["attributes_added"],
            ",".join(ignored) or "-", saved.get("schema_version"), user_id or "-",
        )
        return {"dry_run": False, "summary": summary, "warnings": warnings, **saved}

    # ── 标脏（纯计算）─────────────────────────────────────────────

    @staticmethod
    def _declared_index(rows) -> dict[str, dict]:
        """`{data_source_id: {"entities": {实体名: {属性名}}, "relations": {关系名}}}`。

        实体名下的属性集合供**属性来源**标脏（`attribute_gone`）、关系名集合供
        **背书**标脏（`relation_gone`），都取**当下**的源 schema（compiled schema
        的 `entity_types` / `relation_types`）。属性集合同时收逻辑名 `name` 与物理名
        `physical_name`（D11：抽象 schema 的落点可能存两者之一，只收逻辑名会把
        存物理名的有效落点静默误标 `attribute_gone`）。

        知识库级那行（`data_source_id IS NULL`）不参与：抽象概念只挂数据源级，
        拿 KB 级去比对会把「源里明明没有了」判成还在。
        """
        declared: dict[str, dict] = {}
        for r in rows:
            ds_id = getattr(r, "data_source_id", None)
            if not ds_id:
                continue
            slot = declared.setdefault(ds_id, {"entities": {}, "relations": set()})
            for e in (getattr(r, "entity_types", None) or []):
                if not isinstance(e, dict):
                    continue
                name = str(e.get("name") or "").strip()
                if not name:
                    continue
                columns = {
                    str(a.get(key) or "").strip()
                    for a in (e.get("attributes") or [])
                    if isinstance(a, dict)
                    for key in ("name", "physical_name")
                }
                slot["entities"].setdefault(name, set()).update(c for c in columns if c)
            for rel in (getattr(r, "relation_types", None) or []):
                if not isinstance(rel, dict):
                    continue
                rel_name = str(rel.get("name") or "").strip()
                if rel_name:
                    slot["relations"].add(rel_name)
        return declared

    def _annotate_schema_staleness(self, schema, declared, live_sources, ds_names):
        """落点/属性/背书**读时**标脏（决策 7：只标不改）。

        判据链（逐级）：
        - 落点：源不在 → source_gone；实体不在 → entity_gone；
        - 属性来源：源不在 → source_gone；实体不在 → entity_gone；列不在 → attribute_gone；
        - 背书：源不在 → source_gone；关系名不在 → relation_gone。
        计数 = 全部被标脏的条目数（供界面「N 条对齐已失效」）。

        先判 `source_gone`：源都没了再去它的实体/关系集合里查，会一律判成
        entity_gone / relation_gone，而「源被删了」和「实体被改名了」对人的处置
        完全不同。

        **只读不改**：入参 schema 不被修改（返回的是新 dict），DB 更不动 ——
        「不自动改」（决策 7）在这里体现为「读一百次也不会把 stale 写回去」。

        返回体只含 `entities` / `relations` 两个键 —— `stale_count` 由调用方
        （`get`）按计数返回，`schema_version` 在**行**上、不进这个 dict
        （否则保存时版本号会被写进 JSONB 里，和列上的版本漂移）。
        """
        stale_count = 0

        def mark_landing(s: dict) -> dict:
            nonlocal stale_count
            ds = str(s.get("data_source_id") or "")
            ent = str(s.get("entity_name") or "")
            if ds not in live_sources:
                stale, reason = True, "source_gone"
            elif ent not in (declared.get(ds, {}).get("entities") or {}):
                stale, reason = True, "entity_gone"
            else:
                stale, reason = False, None
            if stale:
                stale_count += 1
            return {**s, "source_name": ds_names.get(ds), "stale": stale, "stale_reason": reason}

        entities = []
        for e in _list_field(schema.get("entities")):
            if not isinstance(e, dict):
                # 非 dict 实体做 `{**e}` 是 TypeError，会把 GET 打成 500 ——
                # 旧的标脏实现在这里就是跳过的，新实现照旧宽容。
                continue
            ent = {**e}
            ent["sources"] = [
                mark_landing(s) for s in _list_field(e.get("sources")) if isinstance(s, dict)
            ]
            attrs = []
            for a in _list_field(e.get("attributes")):
                if not isinstance(a, dict):
                    continue
                attr = {**a}
                marked = []
                for s in _list_field(a.get("sources")):
                    if not isinstance(s, dict):
                        continue
                    ds = str(s.get("data_source_id") or "")
                    name = str(s.get("entity_name") or "")
                    col = str(s.get("attribute_name") or "")
                    declared_entities = declared.get(ds, {}).get("entities") or {}
                    if ds not in live_sources:
                        stale, reason = True, "source_gone"
                    elif name not in declared_entities:
                        stale, reason = True, "entity_gone"
                    elif col not in (declared_entities.get(name) or set()):
                        stale, reason = True, "attribute_gone"
                    else:
                        stale, reason = False, None
                    if stale:
                        stale_count += 1
                    marked.append({**s, "stale": stale, "stale_reason": reason})
                attr["sources"] = marked
                attrs.append(attr)
            ent["attributes"] = attrs
            entities.append(ent)

        relations = []
        for r in _list_field(schema.get("relations")):
            if not isinstance(r, dict):
                continue
            rel = {**r}
            backs = []
            for b in _list_field(r.get("backings")):
                if not isinstance(b, dict):
                    continue
                ds = str(b.get("data_source_id") or "")
                name = str(b.get("relation_name") or "")
                if ds not in live_sources:
                    stale, reason = True, "source_gone"
                elif name not in (declared.get(ds, {}).get("relations") or set()):
                    stale, reason = True, "relation_gone"
                else:
                    stale, reason = False, None
                if stale:
                    stale_count += 1
                backs.append({**b, "stale": stale, "stale_reason": reason})
            rel["backings"] = backs
            relations.append(rel)

        return {"entities": entities, "relations": relations}, stale_count

    @staticmethod
    def _document_landing_catalog(entities, *, max_attrs: int = 40) -> list[dict]:
        """抽象实体 → LLM 落点对齐输入（只传结构，不传凭证）。"""
        out: list[dict] = []
        for entity in entities or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            if not name:
                continue
            attributes = []
            for attr in (entity.get("attributes") or [])[:max_attrs]:
                if not isinstance(attr, dict) or not str(attr.get("name") or "").strip():
                    continue
                attributes.append({
                    "name": str(attr["name"]).strip(),
                    **({"display_name": attr["display_name"]} if attr.get("display_name") else {}),
                    **({"description": attr["description"]} if attr.get("description") else {}),
                    **({"type": attr["type"]} if attr.get("type") else {}),
                })
            item = {
                "name": name,
                "description": str(entity.get("description") or ""),
                "aliases": [
                    str(value) for value in (entity.get("aliases") or []) if str(value).strip()
                ],
                "attributes": attributes,
            }
            if entity.get("display_name"):
                item["display_name"] = entity["display_name"]
            out.append(item)
        return out

    @staticmethod
    def _source_landing_catalog(
        bundles: dict, *, max_entities: int = 200, max_attrs: int = 40,
    ) -> tuple[list[dict], list[str]]:
        """数据源实体 → LLM 落点对齐输入；超过上限截断并显式 warning。"""
        out: list[dict] = []
        for ds_id in sorted(bundles or {}):
            bundle = bundles[ds_id]
            for entity_name in sorted((bundle.get("entities") or {})):
                source_entity = bundle["entities"][entity_name]
                attributes = []
                for attr in (source_entity.get("attributes") or [])[:max_attrs]:
                    if not isinstance(attr, dict) or not str(attr.get("name") or "").strip():
                        continue
                    attributes.append({
                        "name": str(attr["name"]).strip(),
                        **({"description": attr["description"]} if attr.get("description") else {}),
                        **({"type": attr["type"]} if attr.get("type") else {}),
                    })
                out.append({
                    "data_source_id": str(ds_id),
                    "source_name": str(bundle.get("name") or ""),
                    "entity_name": str(entity_name),
                    "description": str(source_entity.get("description") or ""),
                    "attributes": attributes,
                })
        warnings: list[str] = []
        if len(out) > max_entities:
            warnings.append(
                f"自动落点对齐的源实体过多（{len(out)} 个），只对齐前 {max_entities} 个；"
                "可用「聚焦生成」缩小范围"
            )
            out = out[:max_entities]
        return out, warnings

    async def _load_document_seed(
        self, tenant_id: str, kb_id: str,
    ) -> tuple[list[dict], list[dict], list[str]]:
        """读**存储的** KB 级 compiled schema 作为文档本体种子。

        为什么直读仓储行、不走 `compiler.get_compiled_schema`：
        后者在有 active 抽象 schema 时返回**抽象投影**——用投影再构造会形成闭环，
        把旧抽象内容当成文档事实。这里要的是文档侧真正落库的那份。
        不触发自动编译（`auto_compile` 参数不存在于仓储读口）。
        """
        async with get_db_session() as session:
            stored = await OntologySchemaRepository(session).get_compiled_schema(
                tenant_id, kb_id,
            )
        if stored is None:
            return [], [], [
                "没有可复用的文档本体 schema（KB 级 compiled schema 不存在），"
                "本次只用结构化数据源构造",
            ]
        schema = stored.to_dict() if hasattr(stored, "to_dict") else stored
        if not isinstance(schema, dict):
            return [], [], ["文档本体 schema 形状异常，已跳过复用"]
        return document_seed_from_compiled(schema)

    async def _load_source_schemas(self, tenant_id: str, kb_id: str):
        """取该 KB 下**活着的数据源**的 schema 行 + 数据源显示名。

        两条过滤规则：

        1. `data_source_id` 要有值 —— 知识库级那份不绑定任何数据源，把它算进来
           会让概念挂在一个不存在的源上（展开时凭空多一条没有源标的候选）。
        2. `data_source_id` 必须在**还活着的数据源**集合里（`list_by_kb` 软删感知，
           取的就是它的 id）。`list_compiled_schemas` 只看 schema 行自己的 status，
           跟数据源还在不在没有关系：删源**不会**顺带删掉那行（那是另一项决策 ——
           残留行不清理，读路径靠 `source_gone` 标注把它暴露出来）。但把它喂给 LLM
           提概念，产出的是**生下来就已经 `source_gone`、永远用不上**的映射，还白白
           撑大输入（实测 267 条里 89 条属于已删源）。清理是别人的事，这里只**不读**它。

        `list_by_kb` 默认 limit=100 会截断，这里显式放大。
        """
        async with get_db_session() as session:
            rows = await OntologySchemaRepository(session).list_compiled_schemas(
                tenant_id, kb_id,
            )
            ds_rows = await KnowledgeDataSourceRepository(session).list_by_kb(
                tenant_id, kb_id, offset=0, limit=500,
            )
        ds_names = {d.id: d.name for d in ds_rows}
        live_sources = set(ds_names)
        return [r for r in rows if r.data_source_id in live_sources], ds_names

    @staticmethod
    def _source_entities(rows, ds_names: dict) -> list[dict]:
        """把各源的 entity_types 铺平成 LLM 输入。

        `source_name` 一并给模型：同一个实体名在两个库里意思可能不同，
        库名（「UC3 生产库」vs「资产库」）是它能用上的最便宜的消歧信号。

        **没有描述的实体补一串列名**（最多 `_ENTITY_COLUMN_SAMPLE` 个）：SQL Server
        这类没有 MS_Description 的库，实体名是模型能拿到的**全部**信号 —— 实测一台
        招聘库 153 个实体里只有 17 个带描述，而两个库之间唯一的重叠（`Users` / `User`）
        就是这么漏掉的（模型只看到「差一个字母的两个名字」，提示词又说拿不准就分开）。

        有描述的**不带**：描述已经是最强信号，而 token 预算有限（实测 242 实体时
        prompt 15088 / completion 20737，输出 32768 是硬上限），把预算花在最缺信号处。
        """
        out: list[dict] = []
        for r in rows:
            ds_id = r.data_source_id
            src_name = ds_names.get(ds_id, "")
            for e in (r.entity_types or []):
                if not isinstance(e, dict):
                    continue
                name = str(e.get("name") or "").strip()
                if not name:
                    continue
                desc = str(e.get("description") or "").strip()
                if not desc:
                    # `display_name` 只有**不等于名字**时才算描述：生成器在没注释的表上
                    # 把它填成表名（实测那台 SQL Server 153 个实体全是 `name` ==
                    # `display_name`），照收等于把名字当语义线索 —— 「有没有描述」会永远
                    # 判真、列名一个都补不上，模型还会把名字抄成概念描述。
                    label = str(e.get("display_name") or "").strip()
                    if label and label != name:
                        desc = label
                item = {
                    "data_source_id": ds_id,
                    "source_name": src_name,
                    "entity_name": name,
                    "description": desc,
                }
                if not desc:
                    cols = _sample_columns(e)
                    if cols:
                        item["columns"] = cols
                out.append(item)
        return out


__all__ = ["AbstractSchemaService", "ABSTRACT_STATUSES"]
