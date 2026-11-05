"""血缘边的 KB 级 CRUD。

**逐条 CRUD**（对比 abstract_schema 的「一库一份 + 乐观锁」）：
所以这里**没有** `expected_version`、**不产生 409**，只有 1001/400（参数或冲突）与 1002/404（缺席）。

**写路径要回编译 schema 校验**：边的两端列必须真的存在于**该数据源**的编译 schema 里
—— 否则写进去就是一条模型用不了的假边（比不写更坏：它会进路径搜索、占掉一条注入位）。

**层内自校验租户**：`list` / `create` / `update` / `delete` 四个方法**第一行**都调
`require_tenant`（CLAUDE.md：服务层和仓储层必须调用 `require_tenant()` 或等效统一校验；
姊妹服务 `ontology_synonym_service` 同款）。`list` / `delete` **不经过任何仓储** ——
少了这一句，租户合法性就整层让给了上游：一旦有调用方绕过网关，就是跨租户读。

**层内自校验 KB**（`list` / `create`，在租户校验之后）：KB 必须存在且属于该租户
（`_ensure_kb` → 1002/404）。少了这一句：`list` 对不存在/他人的 KB 回的是
`{"items": [], "total": 0}`（200），UI 分不清「没有边」与「KB 不对」；`create` 则把
「KB 不存在」错报成「该数据源没有可用的编译 schema」—— **成因说错了**。
（`update` / `delete` 的 `knowledge_base_id` 是**可选**：2026-10-17 起接受它并入行
定位条件 —— 审查 M-8：只按 `(tenant_id, edge_id)` 定位时，同租户内**另一个 KB 的边**
也能被改/删，且现象与正常操作毫无区别。取值可选是为了不动既有调用方；能力层已把
`knowledge_base_id` 列进 `_REQUIRED_FIELDS`，经能力层进来的调用**一定**带上它。
**传了它就得先过 `_ensure_kb`**（2026-10-17 修复批）：声明一个**已软删**的 KB 时，
能力层的空间解析（`_resolve_space_context` 的 SQL 带 `ki.is_deleted=0`）查不到
⇒ 空间/授权闸门**整段跳过**（不是拒绝，是跳过）；而本服务按
`(tenant_id, edge_id, knowledge_base_id)` **仍能命中软删 KB 的活边**（迁移 010 无
外键/级联，全仓也没有清理边的地方）⇒ 边照改照删。补一次 KB 存在性校验即收口。
**「给了」的判据是 `is not None`，不是真值**（2026-10-17 修复批 2，I-1）：`""` 也算
「给了」⇒ 先撞 `_ensure_kb("")` ⇒ 1002 —— 若按真值判断，空串会**同时**跳过 KB 校验
与行定位的 KB 条件，等于把 M-8 收口整条绕掉（审查端到端复现过的安全口子）。）

**并发兜底是迁移 010 的唯一索引 `uq_lg_edge_quadruple`**：查重（`_existing_id`）与
写库（`commit`）之间有 TOCTOU —— 两个并发写都能通过查重，真正挡住的是那条索引。
所以 `commit()` 撞索引抛的 `IntegrityError` 必须在这里就映射成 **1001/400**
「该血缘边已存在（同一四元组）」：⚠️ **不是** 1003/409（前端把 `409 || 1003` 读成
「覆盖确认」，本线全程不产生 409）。不映射的话裸 `IntegrityError` 会一路裸奔到全局
异常处理器 —— 那里没有它的码可翻，用户看到的是「服务器内部错误」500。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from jonex_core.common.config import config
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.tenant import require_tenant

from ..models.lineage_edge import LineageEdge
from .lineage_import import MAX_ENDPOINT_LEN, MAX_NOTE_LEN
from .ontology_data_projection import project_edge_best_effort

logger = logging.getLogger(__name__)

_EDGE_FIELDS = ("from_table", "from_column", "to_table", "to_column")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def annotate_stale(row: dict) -> dict:
    """读时标 `stale`（roadmap P2 #9）：只对机器来源按滚动窗口判定。

    - `origin=platform`（文件导入 / OpenLineage）才可能过期 —— manual / declared /
      comment 是人的声明，不该因为「最近没导入」而失效；
    - 窗口 `LINEAGE_STALE_AFTER_DAYS`（默认 30，`0` = 关闭）；
    - 与抽象 schema 的 stale 同口径：**读时计算、不写回**，读一百次也不会改库。
    """
    days = int(getattr(config, "LINEAGE_STALE_AFTER_DAYS", 30) or 0)
    stale = False
    if days > 0 and str(row.get("origin") or "") == "platform":
        raw = row.get("last_seen_at")
        seen = None
        if raw:
            try:
                seen = datetime.fromisoformat(str(raw))
            except ValueError:
                seen = None
        if seen is not None:
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            stale = seen < _now() - timedelta(days=days)
    row["stale"] = stale
    return row


def normalize_edge_payload(data: dict) -> dict:
    """只归一，不校验存在性（那要连库，在 service 里做）。

    - 四个列级字段 `strip()` 后**必须非空**（表级血缘不许落库，同迁移 010 的 CHECK）；
    - **长度与 DDL 对齐**（端点 255 / note 500）：不在这里挡，超长会走到 commit
      才由 asyncpg 抛 `DataError`，被全局处理器翻成 500「执行失败」——数据不损坏，
      但错误既不透明也不是 1001/400；
    - `origin` **一律压成 `manual`**：档位是「可信度声明」，UI 不许自选
      （`declared` 意味着「这是外键声明」，人工在管理页敲的不配这一档）。
    """
    got = {f: str((data or {}).get(f) or "").strip() for f in _EDGE_FIELDS}
    missing = [f for f, v in got.items() if not v]
    if missing:
        raise InvalidParameterError(f"血缘边的端点字段不能为空：{'、'.join(missing)}")
    for f, v in got.items():
        if len(v) > MAX_ENDPOINT_LEN:
            raise InvalidParameterError(
                f"{f} 超长（最多 {MAX_ENDPOINT_LEN} 字符，本次 {len(v)}）")
    note = (str((data or {}).get("note") or "").strip() or None)
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise InvalidParameterError(
            f"note 超长（最多 {MAX_NOTE_LEN} 字符，本次 {len(note)}）")
    got["origin"] = "manual"
    got["note"] = note
    got["data_source_id"] = str((data or {}).get("data_source_id") or "").strip()
    return got


class LineageEdgeService:
    """KB 级血缘边的增删改查。"""

    def __init__(self, session) -> None:
        self.session = session

    async def _propagate_governance(self, tenant_id: str, kb_id: str,
                                    ds_id: str) -> None:
        """血缘变更后重算治理标签（派生物，best-effort；失败不回滚主流程）。"""
        from .governance.propagation import propagate_for_source
        try:
            await propagate_for_source(self.session, tenant_id, kb_id, ds_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("治理标签传播失败（best-effort）：%s", exc)

    async def _ensure_kb(self, kb_id: str, tenant_id: str) -> None:
        """KB 必须存在且属于该租户（否则 1002/404）—— 照 `ontology_synonym_service._ensure_kb`。

        差异：那边自己 `async with get_db_session()`，所以把 `session` 当参数传；
        本服务由构造器持有 `self.session`，故不重复传参。

        少了这一句的后果分三处：`list` 对不存在/他人的 KB 会回空列表（200）——
        「没有边」与「KB 不对」在响应上完全同形，UI 无从分辨；`create` 则会把
        「KB 不存在」错报成「该数据源没有可用的编译 schema」（成因说错了）；
        `update` / `delete` 收到一个**已软删**的 KB 时不拦 —— 能力层解析不到该 KB
        时空间闸门整段跳过，服务侧却能命中它的活边（2026-10-17 修复批，见模块
        docstring）。
        """
        # 空 id 直接 1002：`BaseRepository.get_required` 的通用文案会拼成
        # 「KnowledgeInfo 不存在: 」（尾部空串），而且同样会落一条 details.id=""。
        # 这是唯一一处「空 id 必然发生」的入口（POST body 的 kb 由能力层透传）。
        if not str(kb_id or "").strip():
            raise ResourceNotFoundError(message="知识库不存在", details={"id": kb_id})
        from ..repository.knowledge_info_repository import KnowledgeInfoRepository
        await KnowledgeInfoRepository(self.session).get_required(kb_id, tenant_id)

    async def _schema_row(self, tenant_id: str, kb_id: str, data_source_id: str):
        """取**该源那一行**编译 schema —— 与 `nl2sql.service._compiled_row` 同源同口径
        （同一仓储、同一方法、同样按 `data_source_id` 精确匹配）。

        抽成独立方法的理由**不只是**整洁：`_assert_columns_in_schema` 要能单测，
        而单测不能连库 —— 留一个可插桩的接缝（用例 patch 本方法即可）。
        """
        from ..repository.ontology_schema_repository import OntologySchemaRepository
        rows = await OntologySchemaRepository(self.session).list_compiled_schemas(
            tenant_id, kb_id)
        return next((r for r in rows if r.data_source_id == data_source_id), None)

    async def _schema_tables(self, tenant_id: str, kb_id: str,
                             data_source_id: str) -> dict[str, set[str]]:
        """该源编译 schema 的 {物理表: {列名}}；找不到那一行 ⇒ 1001。

        `_assert_columns_in_schema` 与 `import_edges` 共用（导入按批一次取表，
        避免逐行重复列 schema）。
        """
        row = await self._schema_row(tenant_id, kb_id, data_source_id)
        if row is None:
            raise InvalidParameterError(f"该数据源没有可用的编译 schema：{data_source_id}")
        by_table: dict[str, set[str]] = {}
        for e in (row.entity_types or []):
            if isinstance(e, dict) and str(e.get("source_object_id") or "").strip():
                by_table[str(e["source_object_id"]).strip()] = {
                    str(a.get("name") or "") for a in (e.get("attributes") or [])
                    if isinstance(a, dict)
                }
        return by_table

    async def schema_tables(self, tenant_id: str, kb_id: str,
                            data_source_id: str) -> dict[str, set[str]]:
        """公开的编译 schema `{物理表: {列名}}`（推送适配器做表名/列名归一用）。"""
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        return await self._schema_tables(tenant_id, kb_id, data_source_id)

    async def _assert_columns_in_schema(self, tenant_id: str, kb_id: str,
                                        data_source_id: str, got: dict) -> None:
        """边的两端列必须真的存在于**该数据源**的编译 schema。

        读口与 `nl2sql.service._compiled_row` **完全同源**（同一仓储、同一方法、
        同样按 `data_source_id` 精确匹配那一行）—— 差别只是这里要判「列在不在」，
        那边是取四列原料。**找不到那一行 ⇒ 拒**：没有编译 schema 就没有列，
        写进去的边必然用不了。

        ⚠️ `list_compiled_schemas` 只返回 `status == "active"` 的行（仓储里写死的），
        所以抽象 schema 被标成 `proposed` 时这里会拿不到源 —— 那是**正确的保守行为**
        （见本线记忆：抽象 schema 被 UI 保存成 proposed 会让 ② 的概念扇出静默缺席）。
        """
        by_table = await self._schema_tables(tenant_id, kb_id, data_source_id)
        for table_key, col_key in (("from_table", "from_column"), ("to_table", "to_column")):
            table, col = got[table_key], got[col_key]
            cols = by_table.get(table)
            if cols is None:
                raise InvalidParameterError(f"编译 schema 里没有表 {table}（端点 {table_key}）")
            if col not in cols:
                raise InvalidParameterError(f"表 {table} 上没有列 {col}（端点 {col_key}）")

    async def _existing_id(self, tenant_id: str, kb_id: str, data_source_id: str,
                           got: dict, *, exclude_id: str | None = None) -> str | None:
        """同一四元组是否已有活边。"""
        from sqlalchemy import select
        stmt = select(LineageEdge.id).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.from_table == got["from_table"],
            LineageEdge.from_column == got["from_column"],
            LineageEdge.to_table == got["to_table"],
            LineageEdge.to_column == got["to_column"],
            LineageEdge.is_deleted == 0,
        )
        if exclude_id:
            stmt = stmt.where(LineageEdge.id != exclude_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _existing_row(self, tenant_id: str, kb_id: str, data_source_id: str,
                            got: dict, *, exclude_id: str | None = None,
                            ) -> LineageEdge | None:
        """同一四元组的**活边行对象**（导入要读/改 `origin`/`note` 才需要整行）。

        与 `_existing_id` 同一套 WHERE（租户 / KB / 源 / 四元组 / 未删），
        区别只是 SELECT 列：那边取 id（CRUD 查重），这边取整行（导入的分档判定）。
        """
        from sqlalchemy import select
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.from_table == got["from_table"],
            LineageEdge.from_column == got["from_column"],
            LineageEdge.to_table == got["to_table"],
            LineageEdge.to_column == got["to_column"],
            LineageEdge.is_deleted == 0,
        )
        if exclude_id:
            stmt = stmt.where(LineageEdge.id != exclude_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, tenant_id: str, kb_id: str, data_source_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        from sqlalchemy import select
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.is_deleted == 0,
        ).order_by(LineageEdge.from_table, LineageEdge.from_column,
                   LineageEdge.to_table, LineageEdge.to_column,
                   # ⚠️ 末尾两键是**全序**保证：不传 data_source_id（KB 全源视图）时，
                   # 两个源里的同一四元组两行只按四个列级字段排序**完全打平** ——
                   # 那两行的先后就是任意的，两次请求顺序可以不一样。
                   LineageEdge.data_source_id, LineageEdge.id)
        if data_source_id:
            stmt = stmt.where(LineageEdge.data_source_id == data_source_id)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return {"items": [annotate_stale(r.to_dict()) for r in rows],
                "total": len(rows)}

    async def create(self, tenant_id: str, data: dict, *, user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        # KB 校验放在归一/取 schema 之前：kb_id 本身不合法时，报的必须是「KB 不存在」，
        # 而不是下游某个环节的次生成因（「该数据源没有可用的编译 schema」）。
        await self._ensure_kb(str(data.get("knowledge_base_id") or ""), tenant_id)
        got = normalize_edge_payload(data)
        if not got["data_source_id"]:
            raise InvalidParameterError("缺少 data_source_id（表名是物理表名，必须指明是哪个源）")
        await self._assert_columns_in_schema(tenant_id, str(data.get("knowledge_base_id") or ""),
                                             got["data_source_id"], got)
        # ⚠️ 重复边用 **1001/400**，不是 1006/409：见 Global Constraints（本 PR 全程不产生 409）
        dup = await self._existing_id(tenant_id, str(data["knowledge_base_id"]),
                                      got["data_source_id"], got)
        if dup:
            raise InvalidParameterError("该血缘边已存在（同一四元组）")
        row = LineageEdge(
            id=str(uuid.uuid4()), tenant_id=tenant_id,
            knowledge_base_id=str(data["knowledge_base_id"]),
            data_source_id=got["data_source_id"],
            from_table=got["from_table"], from_column=got["from_column"],
            to_table=got["to_table"], to_column=got["to_column"],
            origin="manual", note=got["note"], created_by=user_id,
            last_seen_at=_now(),
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # 并发写：查重与插入之间有 TOCTOU，两个调用方都读到「没有」就会双双插入，
            # 后到的撞 `uq_lg_edge_quadruple`。语义与上面那条查重**完全一样** ——
            # 重复边 —— 所以映射成同一个 1001（**不是** 1003：本线全程不产生 409）。
            # 此刻事务已被 PostgreSQL 中止，必须先回滚（后续任何查询都会 PendingRollbackError）；
            # 回滚也保证会话可继续复用（对比 `ontology_abstract_schema_repository` 不自行
            # 回滚 —— 那个仓储只 flush、不 commit，会话生命周期归调用方）。
            await self.session.rollback()
            raise InvalidParameterError("该血缘边已存在（同一四元组）") from exc
        await self.session.refresh(row)
        # 图是派生物：单边写库成功后再收敛该表对（best-effort，失败不改主流程结局）。
        await project_edge_best_effort(
            tenant_id, row.knowledge_base_id, row.data_source_id,
            row.from_table, row.to_table)
        await self._propagate_governance(
            tenant_id, row.knowledge_base_id, row.data_source_id)
        return annotate_stale(row.to_dict())

    async def import_edges(self, tenant_id: str, kb_id: str, data_source_id: str,
                           rows: list[dict], *, user_id: str | None = None) -> dict:
        """批量导入：逐行校验 + 幂等 upsert。

        - **KB 必须存在且属于该租户**（`_ensure_kb`，同 `create` / `list`）：软删的
          KB 在能力层空间解析里根本查不到（`_resolve_space_context` 的 SQL 带
          `ki.is_deleted=0` ⇒ 闸门整段**跳过**），而这里按 `kb_id` 仍能写入活边。
          少了这一句，导入就成了绕开软删 KB 的后门。
        - 新边 → `origin=platform`；
        - 已有同四元组：
          - 既有 origin 信任度 **高于** platform（manual/declared/comment）⇒ **不降级**，计 skipped；
          - 否则升级/改写为 platform（value/derived → platform 是升级），计 imported；
        - **批内重复四元组计 skipped、不重复插入**：`autoflush=False` 时
          `_existing_row` 看不到本批刚 `add()` 的行，同一文件里出现两次相同四元组
          会让两次 `add()` 都落库，commit 撞 `uq_lg_edge_quadruple` ⇒ 整批丢失。
        - 单行坏（缺列/列不存在/超长）计入 `errors`，**不中断整批**。
        - `errors` 只回前 20 条，但 `error_count` 是**全量计数**（同步状态文案用它，
          否则超过 20 条时看起来像「只有 20 个错」）。
        """
        tenant_id = require_tenant(tenant_id)
        # KB 校验放在最前（同 create）：kb_id 不合法时先报 1002，且挡住软删 KB。
        await self._ensure_kb(kb_id, tenant_id)
        from .lineage_import import normalize_import_payload
        from .nl2sql.paths import confidence_of

        by_table = await self._schema_tables(tenant_id, kb_id, data_source_id)
        imported = skipped = 0
        errors: list[str] = []
        # 批内已见过的归一四元组。只有**通过校验**的行才登记 —— 同一坏行出现两次
        # 应该各记一条 error，而不是第二次被当成「重复」跳掉。
        seen: set[tuple[str, str, str, str]] = set()
        platform_conf = confidence_of("platform")
        for i, row in enumerate(rows or [], 1):
            try:
                got = normalize_import_payload(row, data_source_id)
                for table_key, col_key in (("from_table", "from_column"),
                                           ("to_table", "to_column")):
                    table, col = got[table_key], got[col_key]
                    cols = by_table.get(table)
                    if cols is None:
                        raise InvalidParameterError(
                            f"编译 schema 里没有表 {table}（端点 {table_key}）")
                    if col not in cols:
                        raise InvalidParameterError(
                            f"表 {table} 上没有列 {col}（端点 {col_key}）")
                quad = (got["from_table"], got["from_column"],
                        got["to_table"], got["to_column"])
                if quad in seen:
                    # 同一文件里的重复下发：第一条已经建/升级过了，这条按「已有」计。
                    skipped += 1
                    continue
                seen.add(quad)
                existing = await self._existing_row(tenant_id, kb_id, data_source_id, got)
                if existing is not None:
                    if confidence_of(str(existing.origin or "")) > platform_conf:
                        # 更高信任度的来源（人工/声明/注释）拥有这条边：
                        # 不降级，也**不刷新**它的 last_seen（平台事件不是它的确认者）。
                        skipped += 1
                        continue
                    # 重复摄入 = 来源再次确认这条边：刷新新鲜度，即使内容没变
                    # （否则「ETL 一直稳定推送」也会在 30 天后被误标 stale）。
                    existing.last_seen_at = _now()
                    # 幂等：已经就是 platform 且没有新备注 ⇒ 什么都没改，计 skipped。
                    # 旧实现无条件 `imported += 1`，同一份 JSON 导两次会报
                    # `imported=1, skipped=1`（第二次的「导入」是假的）。
                    changed = (
                        existing.origin != "platform"
                        or (bool(got["note"]) and existing.note != got["note"])
                    )
                    if changed:
                        existing.origin = "platform"
                        if got["note"]:
                            existing.note = got["note"]
                        imported += 1
                    else:
                        skipped += 1
                    continue
                self.session.add(LineageEdge(
                    id=uuid.uuid4().hex, tenant_id=tenant_id,
                    knowledge_base_id=kb_id, data_source_id=data_source_id,
                    from_table=got["from_table"], from_column=got["from_column"],
                    to_table=got["to_table"], to_column=got["to_column"],
                    origin="platform", note=got["note"], created_by=user_id,
                    last_seen_at=_now(),
                ))
                imported += 1
            except InvalidParameterError as exc:
                errors.append(f"row {i}: {exc}")
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # 批内查重只能挡住「本批已知的重复」；跨进程并发（本批查重之后、commit
            # 之前别人落了同四元组）仍会撞 `uq_lg_edge_quadruple`。语义与重复边
            # 相同 ⇒ 1001/400（**不是** 1003/409，本线不产生 409）。先回滚：事务
            # 已被 PostgreSQL 中止，不回滚则会话后续查询全部 PendingRollbackError。
            await self.session.rollback()
            raise InvalidParameterError("血缘边导入冲突，请重试") from exc
        await self._propagate_governance(tenant_id, kb_id, data_source_id)
        return {"imported": imported, "skipped": skipped,
                "errors": errors[:20], "error_count": len(errors),
                "total": len(rows or [])}

    async def update(self, tenant_id: str, edge_id: str, data: dict,
                     *, user_id: str | None = None,
                     knowledge_base_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            # 修复批（2026-10-17）：声明的 KB **已软删**时，能力层的空间解析（SQL 带
            # `ki.is_deleted=0`）查不到 ⇒ 空间/授权闸门**整段跳过**（不是拒绝），而本
            # 方法按 `(tenant_id, edge_id, knowledge_base_id)` 仍能命中活边 ——
            # 迁移 010 无外键/级联、全仓不清边，「KB 已软删、边还活着」是可达状态。
            # 补一次 KB 存在性校验（`get_required` 带软删条件 ⇒ 软删 KB 直接 1002/404）。
            # **只在「给了」时调（`is not None`）**：不传 KB（None）的既有调用方
            # （Task 2 那批用例）行为不变；**空串算「给了」**（修复批 2，I-1）——
            # `""` 必须先撞 `_ensure_kb("")` ⇒ 1002，按真值跳过就是原安全口子。
            await self._ensure_kb(knowledge_base_id, tenant_id)
        from sqlalchemy import select
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id, LineageEdge.id == edge_id,
            LineageEdge.is_deleted == 0)
        if knowledge_base_id is not None:
            # 审查 M-8：只按 (tenant_id, edge_id) 定位时，**同租户内另一个 KB 的边**
            # 也能被改 —— 现象与正常更新一模一样（200 + 行被改），不会报任何错。
            # 取值**可选**是为了不动既有调用方（Task 2 那批用例不传 KB）；能力层已把它
            # 列进 `_REQUIRED_FIELDS` ⇒ 经能力层进来的调用**一定**带上它。
            # ⚠️ 判据是 `is not None`（修复批 2，I-1）：`""` 若被当成「没给」跳过本条件，
            # 等于把 M-8 的收口整个绕掉（同租户内只要有 edge_id 就能改别的 KB 的边）。
            stmt = stmt.where(LineageEdge.knowledge_base_id == knowledge_base_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            # 边不存在 → 1002/404（**不是 409**：没有「覆盖确认」语义）
            raise ResourceNotFoundError("血缘边不存在")
        # 图收敛需要**旧**四元组：改动之前先存副本（表对变了要清理旧边）。
        old_edge = {f: str(getattr(row, f)) for f in _EDGE_FIELDS}
        old_kb_id = row.knowledge_base_id
        old_ds_id = row.data_source_id
        got = normalize_edge_payload({**row.to_dict(), **(data or {})})
        # ⚠️ 边**不能换源**：表名是物理表名，换源等于换了一张表，语义上是另一条边。
        # 这里必须**显式拒绝**而不是忽略 —— 静默忽略是本线最忌的「悄悄放行」：
        # 调用方以为改成功了，库里纹丝不动。
        if got["data_source_id"] and got["data_source_id"] != (row.data_source_id or ""):
            raise InvalidParameterError("血缘边不能更换数据源（请删除后在新源上重建）")
        # ⚠️ **降级是有条件的**：`normalize_edge_payload` 一律给 `manual`，但更新一条
        # **非手工来源**（如将来 OpenLineage 摄进来的 `platform` 边）时，只改个备注就把
        # 它永久降档 = 丢信息。规则：**只有列四元组真的变了**（人改了映射本身）才降级，
        # 只动 `note` 则保留原 `origin`。
        if all(str(getattr(row, f)) == got[f] for f in _EDGE_FIELDS):
            got["origin"] = row.origin or "manual"
        await self._assert_columns_in_schema(tenant_id, row.knowledge_base_id,
                                             row.data_source_id or got["data_source_id"], got)
        dup = await self._existing_id(tenant_id, row.knowledge_base_id,
                                      row.data_source_id or got["data_source_id"], got,
                                      exclude_id=edge_id)
        if dup:
            raise InvalidParameterError("该血缘边已存在（同一四元组）")
        for f in _EDGE_FIELDS:
            setattr(row, f, got[f])
        row.note = got["note"]
        # ⚠️ `origin` **也要写回** —— 否则上面那条条件降级规则「算了却没人用」，
        # 非手工来源的边被改了列之后档位还留在原处（假声明）。
        row.origin = got["origin"]
        # 人工编辑也是对当前边的再次确认：刷新新鲜度（否则改过备注的 platform 边
        # 仍按旧 last_seen_at 被标 stale）。
        row.last_seen_at = _now()
        try:
            await self.session.commit()
        except IntegrityError as exc:
            # 同 `create` 的 I-1：改四元组同样有 TOCTOU（`_existing_id(exclude_id=…)` 之后、
            # commit 之前，别人落了一条同四元组的活边），撞的还是 `uq_lg_edge_quadruple`。
            # 这条 UPDATE 只写这几列，实际能撞上的也只有那条索引（其余 CHECK 已由
            # `normalize_edge_payload` 提前挡住）。
            await self.session.rollback()
            raise InvalidParameterError("该血缘边已存在（同一四元组）") from exc
        await self.session.refresh(row)
        # 图是表级聚合：只有四元组真的变了才重算。只改 note ⇒ 图无变化，不碰。
        if any(old_edge[f] != str(getattr(row, f)) for f in _EDGE_FIELDS):
            old_pair = (old_edge["from_table"], old_edge["to_table"])
            new_pair = (str(row.from_table), str(row.to_table))
            if old_pair != new_pair:
                # 旧表对按**当前活行**重算：该行已经不在了 ⇒ 没有对应边 ⇒ 删旧边。
                await project_edge_best_effort(
                    tenant_id, old_kb_id, old_ds_id, *old_pair)
            await project_edge_best_effort(
                tenant_id, row.knowledge_base_id, row.data_source_id, *new_pair)
        await self._propagate_governance(
            tenant_id, row.knowledge_base_id, row.data_source_id)
        return annotate_stale(row.to_dict())

    async def delete(self, tenant_id: str, edge_id: str,
                     *, knowledge_base_id: str | None = None) -> bool:
        tenant_id = require_tenant(tenant_id)
        if knowledge_base_id is not None:
            # 同 `update` 的软删 KB 收口（修复批 2026-10-17）：不先校验 KB，一个**已软删**
            # 的 KB 会让能力层整段跳过空间/授权闸门，而这里的行定位照样命中活边 ⇒ 边被删。
            # 判据 `is not None`（修复批 2，I-1）：空串算「给了」⇒ 先撞 `_ensure_kb("")` ⇒ 1002。
            await self._ensure_kb(knowledge_base_id, tenant_id)
        from sqlalchemy import select
        stmt = select(LineageEdge).where(
            LineageEdge.tenant_id == tenant_id, LineageEdge.id == edge_id,
            LineageEdge.is_deleted == 0)
        if knowledge_base_id is not None:
            # 同 `update` 的 M-8 收口：不并入 KB 条件时，**同租户内另一个 KB 的边**
            # 会被真的（软）删掉，而调用方以为删的是自己库里的那条。
            # ⚠️ 判据同 update：`is not None`（空串若被当成「没给」跳过本条件 = 绕掉 M-8）。
            stmt = stmt.where(LineageEdge.knowledge_base_id == knowledge_base_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("血缘边不存在")
        kb_id = row.knowledge_base_id
        ds_id = row.data_source_id
        from_table, to_table = row.from_table, row.to_table
        row.is_deleted = 1
        await self.session.commit()
        # 删除一行后按**剩余活行**重算该表对（同表对可能还有别的列映射），
        # 一行不剩才删图边（`upsert_edge` 的「回到当前真相」语义）。
        await project_edge_best_effort(tenant_id, kb_id, ds_id, from_table, to_table)
        await self._propagate_governance(tenant_id, kb_id, ds_id)
        return True

    async def delete_platform_edges(self, tenant_id: str, kb_id: str,
                                    data_source_id: str, *,
                                    from_table: str | None = None,
                                    to_table: str | None = None) -> int:
        """平台删除事件：软删该源里匹配的 **platform** 边。

        - `from_table` / `to_table` 为空表示不限那一端（OM 删除事件带两端、
          DataHub 的 aspect 整体删除只带下游表）；
        - **只动 `origin=platform`**：人工 / 声明 / 注释来源的边不归平台管；
        - 删除后按剩余活行重算图投影与治理（与单条 `delete` 同口径）。
        """
        tenant_id = require_tenant(tenant_id)
        await self._ensure_kb(kb_id, tenant_id)
        from sqlalchemy import select, update
        conditions = [
            LineageEdge.tenant_id == tenant_id,
            LineageEdge.knowledge_base_id == kb_id,
            LineageEdge.data_source_id == data_source_id,
            LineageEdge.is_deleted == 0,
            LineageEdge.origin == "platform",
        ]
        if from_table:
            conditions.append(LineageEdge.from_table == from_table)
        if to_table:
            conditions.append(LineageEdge.to_table == to_table)
        pairs = (await self.session.execute(
            select(LineageEdge.from_table, LineageEdge.to_table)
            .where(*conditions)
            .distinct()
        )).all()
        if not pairs:
            return 0
        result = await self.session.execute(
            update(LineageEdge).where(*conditions).values(is_deleted=1))
        await self.session.commit()
        for from_t, to_t in pairs:
            await project_edge_best_effort(
                tenant_id, kb_id, data_source_id, from_t, to_t)
        await self._propagate_governance(tenant_id, kb_id, data_source_id)
        return int(result.rowcount or 0)
