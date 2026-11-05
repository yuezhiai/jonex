"""结构化数据 → Neo4j 数据资产子图（P1.5a，spec §三）。

- 权威：per-source active compiled schema + `lineage_edges`；图是派生物。
- 表=节点（列=attributes），血缘=表级 `DERIVED_FROM` + `column_map`。
- 全量重建幂等；单边 upsert/delete 收敛（都以**当前活行**为准重算）。
- P1.5b：`rebuild_alignments` 把**仅 active** 的跨模态对齐行投影成文档实体
  → 数据源表节点的 `MAPS_TO` 边（同样先清后挂、可重建）。
- **fail-open 由调用方兜**：本模块的 `rebuild_source` / `rebuild_alignments` 会抛，
  触发点统一走 `project_*_best_effort` / `rebuild_alignments_best_effort`。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.neo4j_client import get_neo4j_driver
from jonex_core.common.tenant import require_tenant

from ..models.lineage_edge import LineageEdge
from ..repository.ontology_data_alignment_repository import (
    OntologyDataAlignmentRepository,
)
from ..repository.ontology_data_asset_repository import OntologyDataAssetRepository
from ..repository.ontology_schema_repository import OntologySchemaRepository

logger = logging.getLogger(__name__)


def _attr_list(entity: dict) -> list[dict]:
    return [a for a in (entity.get("attributes") or []) if isinstance(a, dict)]


def _require_data_source_id(data_source_id: Optional[str]) -> str:
    """数据源 id 必填：空/空白会让 `None == None` 命中知识库级 compiled schema。"""
    if not str(data_source_id or "").strip():
        raise InvalidParameterError("缺少 data_source_id")
    return data_source_id


def build_desired(schema: dict, edge_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """compiled schema + 血缘行 → (期望节点, 期望边)。纯函数。

    节点按物理表去重：多个实体映射到同一张表时只产出一个节点，**保留最后出现
    的那一项**（schema 内后声明的实体视为对该表的更新）。
    """
    # ⚠️ import 放在函数内是**为了打断包初始化期的循环**：本模块被
    # `data_source_service` 在模块作用域 import，而 `.nl2sql` 包的 `__init__`
    # 会导入 `nl2sql.service`，后者又 `from ..data_source_service import
    # DataSourceService` —— 模块级 import 会在 DataSourceService 尚未定义时撞上
    # 半初始化的模块（ImportError）。调用期（build_desired 只在运行期被调）再导入，
    # 届时包已完整加载，无环。
    from .nl2sql.paths import confidence_of

    version = (schema or {}).get("schema_version")
    nodes_by_table: dict[str, dict] = {}
    tables: set[str] = set()
    for e in (schema or {}).get("entity_types") or []:
        if not isinstance(e, dict):
            continue
        table = str(e.get("source_object_id") or "").strip()
        if not table:
            # 没有物理表的实体（概念/知识库级）不进数据资产子图。
            continue
        tables.add(table)
        nodes_by_table.pop(table, None)
        nodes_by_table[table] = {"table": table,
                                 "entity_name": str(e.get("name") or "").strip(),
                                 "attributes": _attr_list(e),
                                 "schema_version": version}
    nodes = list(nodes_by_table.values())
    grouped: dict[tuple[str, str], dict] = {}
    for r in edge_rows or []:
        ft = str((r or {}).get("from_table") or "").strip()
        tt = str((r or {}).get("to_table") or "").strip()
        fc = str((r or {}).get("from_column") or "").strip()
        tc = str((r or {}).get("to_column") or "").strip()
        if not (ft and tt and fc and tc):
            continue
        # 端点不在本源 schema 的边丢弃：图里没有节点可挂（这也是「编译 schema 权威」的体现）
        if ft not in tables or tt not in tables:
            continue
        key = (ft, tt)
        cur = grouped.get(key)
        origin = str((r or {}).get("origin") or "")
        if cur is None:
            cur = {"from_table": ft, "to_table": tt, "column_map": [],
                   "origin": origin, "confidence": confidence_of(origin)}
            grouped[key] = cur
        elif confidence_of(origin) > cur["confidence"]:
            # 同一表对的多条边合并成一条；origin 取信任度最高的一档。
            cur["origin"] = origin
            cur["confidence"] = confidence_of(origin)
        pair = {"from_column": fc, "to_column": tc}
        if pair not in cur["column_map"]:
            cur["column_map"].append(pair)
    return nodes, list(grouped.values())


def build_alignment_desired(
    alignment_rows: list,
    schema_rows: list,
) -> tuple[list[dict], int]:
    """active 对齐行 + 各源 compiled schema → (期望 `MAPS_TO` 三元组, unresolved)。

    纯函数（无 IO），钉住解析口径：

    - `(data_source_id, entity_name) → source_object_id` 只认**数据源级** schema
      （`data_source_id` 为空的知识库级那份跳过，否则会制造永远解析不到的键）；
    - 解析不到物理表 / 业务键空白的行计入 `unresolved`，**不传给仓储**；
    - 输出按 `(doc_type, data_source_id, table)` 去重（多个实体名映射同一张表时
      边是同一条，重复 MERGE 没有意义）。
    """
    source_tables: dict[tuple[str, str], str] = {}
    for row in schema_rows or []:
        ds_id = str(getattr(row, "data_source_id", None) or "").strip()
        if not ds_id:
            continue
        for entity in getattr(row, "entity_types", None) or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            table = str(entity.get("source_object_id") or "").strip()
            if name and table:
                source_tables[(ds_id, name)] = table

    desired: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    unresolved = 0
    for row in alignment_rows or []:
        doc_type = str(getattr(row, "doc_type", None) or "").strip()
        ds_id = str(getattr(row, "data_source_id", None) or "").strip()
        entity_name = str(getattr(row, "entity_name", None) or "").strip()
        table = source_tables.get((ds_id, entity_name), "") if (
            ds_id and entity_name) else ""
        if not (doc_type and ds_id and table):
            unresolved += 1
            continue
        key = (doc_type, ds_id, table)
        if key in seen:
            continue
        seen.add(key)
        desired.append({"doc_type": doc_type,
                        "data_source_id": ds_id, "table": table})
    return desired, unresolved


class OntologyDataProjectionService:
    def __init__(self) -> None:
        self._repo = OntologyDataAssetRepository(get_neo4j_driver())

    async def _load_schema(self, tenant_id: str, kb_id: str,
                           ds_id: str) -> Optional[dict]:
        """该数据源的 active compiled schema（没有 → None）。

        `list_compiled_schemas` 只返回 active；知识库级行（`data_source_id=None`）
        不属于任何源，不参与本源的投影。
        """
        async with get_db_session() as session:
            rows = await OntologySchemaRepository(session).list_compiled_schemas(
                tenant_id, kb_id)
        for r in rows:
            if getattr(r, "data_source_id", None) == ds_id:
                return r.to_dict()
        return None

    async def _load_edges(self, tenant_id: str, kb_id: str,
                          ds_id: str) -> list[dict]:
        async with get_db_session() as session:
            stmt = select(LineageEdge).where(
                LineageEdge.tenant_id == tenant_id,
                LineageEdge.knowledge_base_id == kb_id,
                LineageEdge.data_source_id == ds_id,
                LineageEdge.is_deleted == 0,
            ).order_by(LineageEdge.id)
            rows = list((await session.execute(stmt)).scalars().all())
        return [{"from_table": r.from_table, "from_column": r.from_column,
                 "to_table": r.to_table, "to_column": r.to_column,
                 "origin": r.origin} for r in rows]

    async def _load_active_alignments(
        self, tenant_id: str, kb_id: str,
    ) -> list:
        """加载 active 对齐行（快速跳过判据，先于 compiled schema 加载）。

        **只读 active**：proposed 是候选、rejected 是人否过的，二者都不能进图。
        """
        async with get_db_session() as session:
            return await OntologyDataAlignmentRepository(session).list_by_kb(
                tenant_id, kb_id, status="active")

    async def _load_compiled_schemas(
        self, tenant_id: str, kb_id: str,
    ) -> list:
        """一次加载各源 compiled schema：解析口径见 `build_alignment_desired`。"""
        async with get_db_session() as session:
            return await OntologySchemaRepository(
                session).list_compiled_schemas(tenant_id, kb_id)

    async def rebuild_alignments(self, tenant_id: str, kb_id: str) -> dict:
        """把该 KB 的 `MAPS_TO` 边重建成「active 对齐行」的当前形态（幂等）。

        返回 `{edges, merge_calls, unresolved, alignments, no_instances}`：
        `edges` 是重建后图里的**实际边数**；`merge_calls` 是执行过的 MERGE 次数
        （期望三元组的文档实体不存在时空转，两者会分叉）；`unresolved` 是解析
        不到物理表的 active 行数；`alignments` 是 active 行总数（含 unresolved）；
        `no_instances` 是期望里出现、但重建后没有任何 `MAPS_TO` 边的 `doc_type`
        数 —— 零边的成因可以是**文档侧实体不存在**，**或**数据源表节点不存在
        （MERGE 两端任一端缺失都会空转）；unresolved 行本来就不进期望集，
        不参与此计数。

        **没有 active 行时先数一遍边**：图里没有 `MAPS_TO` → 返回 `skipped` 且
        **不调** `rebuild_maps_to`；还有陈旧边 → 传空期望集清掉（否则撤销最后
        一条对齐后旧边会永远留在图里）。这两种情况都**不加载编译 schema**：
        快速跳过不需要它，清旧边只需要空期望集。
        """
        tenant_id = require_tenant(tenant_id)
        rows = await self._load_active_alignments(tenant_id, kb_id)
        if not rows:
            if await self._repo.count_maps_to(tenant_id, kb_id) == 0:
                return {"edges": 0, "merge_calls": 0, "unresolved": 0,
                        "alignments": 0, "no_instances": 0,
                        "skipped": "no_active_alignments"}
            result = await self._repo.rebuild_maps_to(tenant_id, kb_id, [])
            return {"edges": int((result or {}).get("edges") or 0),
                    "merge_calls": int((result or {}).get("merge_calls") or 0),
                    "unresolved": 0, "alignments": 0, "no_instances": 0}
        schemas = await self._load_compiled_schemas(tenant_id, kb_id)
        desired, unresolved = build_alignment_desired(rows, schemas)
        result = await self._repo.rebuild_maps_to(tenant_id, kb_id, desired)
        desired_doc_types = {d["doc_type"] for d in desired}
        matched_doc_types = set((result or {}).get("matched_doc_types") or [])
        return {"edges": int((result or {}).get("edges") or 0),
                "merge_calls": int((result or {}).get("merge_calls") or 0),
                "unresolved": unresolved,
                "alignments": len(rows),
                "no_instances": (len(desired_doc_types)
                                 - len(desired_doc_types & matched_doc_types))}

    async def rebuild_source(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> dict:
        """把该源子图重建成「schema + 活血缘行」的当前形态（幂等）。

        没有 active compiled schema 时**不删图、不抛**：返回 skipped，由调用方
        决定语义（源刚建/还未生成 schema 是正常状态）。真正失败（DB/Neo4j）会抛，
        best-effort 由调用点兜。
        """
        tenant_id = require_tenant(tenant_id)
        data_source_id = _require_data_source_id(data_source_id)
        schema = await self._load_schema(tenant_id, kb_id, data_source_id)
        if schema is None:
            return {"skipped": "no_active_compiled_schema", "nodes": 0, "edges": 0}
        nodes, edges = build_desired(
            schema, await self._load_edges(tenant_id, kb_id, data_source_id))
        want_nodes = {n["table"] for n in nodes}
        want_edges = {(e["from_table"], e["to_table"]) for e in edges}
        existing_nodes = {n["table"] for n in
                          await self._repo.list_nodes(tenant_id, kb_id, data_source_id)}
        existing_edges = {(e["from_table"], e["to_table"]) for e in
                          await self._repo.list_edges(tenant_id, kb_id, data_source_id)}
        # 先删陈旧边（指向将被删节点的边必须先走），再删陈旧节点。
        for ft, tt in existing_edges - want_edges:
            await self._repo.delete_edge(tenant_id, kb_id, data_source_id, ft, tt)
        for table in existing_nodes - want_nodes:
            await self._repo.delete_node(tenant_id, kb_id, data_source_id, table)
        # merge_edge 依赖两端节点已存在，所以节点必须先写。
        for n in nodes:
            await self._repo.merge_node(tenant_id, kb_id, data_source_id, **n)
        for e in edges:
            await self._repo.merge_edge(tenant_id, kb_id, data_source_id,
                                        e["from_table"], e["to_table"],
                                        e["column_map"], e["origin"],
                                        e["confidence"])
        return {"nodes": len(nodes), "edges": len(edges),
                "deleted_nodes": len(existing_nodes - want_nodes),
                "deleted_edges": len(existing_edges - want_edges),
                "schema_version": schema.get("schema_version")}

    async def upsert_edge(self, tenant_id: str, kb_id: str, data_source_id: str,
                          from_table: str, to_table: str) -> None:
        """按当前活行重算该表对的图边；一行不剩 → 删边。

        列级血缘行可能有多条（同一表对的多个列映射），图边是表级聚合：任何一条
        行变更后都要用**剩余活行**重建 `column_map`，而不是增量改单条。

        与 `rebuild_source` 一致：**没有 active compiled schema 时直接返回**。
        活行不能单独构成图（端点必须来自 schema），此时删边会让图比权威源更早
        清空；宁可不动，等下一次全量重建收敛。
        """
        tenant_id = require_tenant(tenant_id)
        data_source_id = _require_data_source_id(data_source_id)
        schema = await self._load_schema(tenant_id, kb_id, data_source_id)
        if schema is None:
            return
        rows = [r for r in await self._load_edges(tenant_id, kb_id, data_source_id)
                if r["from_table"] == from_table and r["to_table"] == to_table]
        _, edges = build_desired(schema, rows)
        if not edges:
            await self._repo.delete_edge(tenant_id, kb_id, data_source_id,
                                         from_table, to_table)
            return
        e = edges[0]
        await self._repo.merge_edge(tenant_id, kb_id, data_source_id,
                                    e["from_table"], e["to_table"],
                                    e["column_map"], e["origin"],
                                    e["confidence"])

    async def delete_edge(self, tenant_id: str, kb_id: str, data_source_id: str,
                          from_table: str, to_table: str) -> None:
        """列级行删除后的收敛入口 —— 语义同 `upsert_edge`（都按活行重算）。

        保留独立名字是为了调用点可读：删除一行血缘后不要直接删图边（同表对可能
        还有别的列映射），而是回到「当前真相」。
        """
        tenant_id = require_tenant(tenant_id)
        data_source_id = _require_data_source_id(data_source_id)
        await self.upsert_edge(tenant_id, kb_id, data_source_id,
                               from_table, to_table)

    async def delete_source(self, tenant_id: str, kb_id: str,
                            data_source_id: str) -> None:
        tenant_id = require_tenant(tenant_id)
        data_source_id = _require_data_source_id(data_source_id)
        await self._repo.delete_scope(tenant_id, kb_id, data_source_id)


# ==================== 触发点用的 best-effort 包装 ====================
# 投影是**增能项**：主流程（编译 / 导入 / OL / 单边 CRUD / 源删除）先成功、再投影；
# 投影的任何失败（DB / Neo4j / 参数）都只记 warning，绝不改变主流程的结局。

async def project_source_best_effort(tenant_id: str, kb_id: str,
                                     data_source_id: str) -> None:
    """全量重建某源的子图；失败只告警。"""
    if not config.ONTOLOGY_DATA_PROJECTION_ENABLED:
        logger.info("数据资产投影开关关闭，跳过：ds=%s", data_source_id)
        return
    try:
        await OntologyDataProjectionService().rebuild_source(
            tenant_id, kb_id, data_source_id)
    except Exception as exc:  # noqa: BLE001 —— 投影是增能项
        logger.warning("数据资产投影失败（不影响主流程）：ds=%s err=%s",
                       data_source_id, exc)


async def project_edge_best_effort(tenant_id: str, kb_id: str, data_source_id: str,
                                   from_table: str, to_table: str) -> None:
    """按当前活行重算一个表对的图边（无边则删）；失败只告警。"""
    if not config.ONTOLOGY_DATA_PROJECTION_ENABLED:
        return
    try:
        await OntologyDataProjectionService().upsert_edge(
            tenant_id, kb_id, data_source_id, from_table, to_table)
    except Exception as exc:  # noqa: BLE001 —— 投影是增能项
        logger.warning("数据资产单边投影失败：%s->%s err=%s",
                       from_table, to_table, exc)


async def project_source_delete_best_effort(tenant_id: str, kb_id: str,
                                            data_source_id: str) -> None:
    """源删除后的图清理；失败只告警。"""
    if not config.ONTOLOGY_DATA_PROJECTION_ENABLED:
        return
    try:
        await OntologyDataProjectionService().delete_source(
            tenant_id, kb_id, data_source_id)
    except Exception as exc:  # noqa: BLE001 —— 投影是增能项
        logger.warning("数据资产源清理失败：ds=%s err=%s", data_source_id, exc)


async def rebuild_alignments_best_effort(tenant_id: str, kb_id: str) -> None:
    """重建跨模态对齐边的触发点包装：开关 + 吞异常，失败只告警。"""
    if not config.ONTOLOGY_DATA_PROJECTION_ENABLED:
        logger.info("跨模态对齐投影开关关闭，跳过：kb=%s", kb_id)
        return
    try:
        await OntologyDataProjectionService().rebuild_alignments(
            tenant_id, kb_id)
    except Exception as exc:  # noqa: BLE001 —— 投影是增能项
        logger.warning("跨模态对齐投影失败（不影响主流程）：kb=%s err=%s",
                       kb_id, exc)
