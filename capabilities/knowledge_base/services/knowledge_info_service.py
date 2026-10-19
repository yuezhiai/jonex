#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KnowledgeInfo CRUD service."""
import logging
import uuid

logger = logging.getLogger(__name__)

from sqlalchemy import or_, select, text

from jonex_core.common import get_db_session
from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.exceptions import (
    InvalidParameterError,
    PermissionDeniedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.data_source import KnowledgeDataSource
from ..models.knowledge_info import KnowledgeInfo
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from ..repository.knowledge_info_repository import (
    KnowledgeInfoRepository,
    cleanup_kb_residue,
)
# [jonex] 权限重构 B3：KB 资源身份取值的**唯一定义点**在 kb_permission_service。
# 这里 import 常量而非另写字面量 —— 原先本文件里散落着 ("kb_manager","editor","viewer")
# 三处独立的元组/字典，加减层级时必然漏改。
from .kb_permission_service import KB_MANAGER, KB_MEMBER, _KB_ROLE_RANK
# [jonex] kb_type 常量模块级导入：create() 与 get() 两处都要用 LLM_WIKI_TYPES，
# 不能只放在 create() 里做局部 import（get() 第 247 行会 NameError）。
# ⚠ `VALID_KB_TYPES` 不在这里 import：create() 的校验已收敛进 `resolve_creatable_kb_type()`
# （两级校验 + 两个错误码），本文件不再直接判集合，避免又出现第二个判定点。
from .kb_type_service import LLM_WIKI_TYPES, resolve_creatable_kb_type

# [jonex] 知识库名称 / 描述长度上限（2026-10-16）。**唯一判定点**，create / update 共用。
#
# 与 DB 列宽（`knowledge_info.name` VARCHAR(255) / `description` TEXT）不是一回事：
#   · > 255 的名称撞 DB 列宽，asyncpg 抛 DataError，一路冒到调用方只剩「执行失败」；
#   · 51~255 的名称**静默成功**落库 —— 越过了产品口径却没人拦。
# 50 / 200 是产品口径，必须由服务层给可读文案；DB 列宽不动（存量数据可能更长，
# 收窄列宽只会把"报错"从应用层挪回 DB 层）。
KB_NAME_MAX_LENGTH = 50
KB_DESCRIPTION_MAX_LENGTH = 200


def _check_text_lengths(name: str | None = None, description: str | None = None) -> None:
    """名称 / 描述超长即抛 `InvalidParameterError`（业务可读文案，不是「执行失败」）。

    ⚠ 名称按 **strip 后**长度判定：create / update 都先 strip 再落库（见 create 内注释），
    这里若按原始串判，`"<50 字> "`（尾随空格）会被拦，而它入库后正好 50 字 —— 写入口径
    与校验口径分叉。描述原样判定（写入路径也不 strip）。

    非字符串值直接跳过：那不是"长度"问题，交给下游既有的类型路径，不在这里改口径。
    """
    checks = (
        (name.strip() if isinstance(name, str) else "",
         KB_NAME_MAX_LENGTH, "err.kb.name_too_long", "知识库名称"),
        (description if isinstance(description, str) else "",
         KB_DESCRIPTION_MAX_LENGTH, "err.kb.description_too_long", "知识库描述"),
    )
    for value, max_length, msg_key, label in checks:
        if len(value) > max_length:
            raise InvalidParameterError(
                message=translate(
                    msg_key,
                    params={"max": max_length, "actual": len(value)},
                    fallback=f"{label}不能超过 {max_length} 个字符（当前 {len(value)} 个）",
                )
            )


class KnowledgeInfoService:
    """知识库信息 CRUD"""

    async def _ensure_name_unique(
        self,
        session,
        tenant_id: str,
        space_id: str,
        name: str,
        exclude_id: str | None = None,
    ) -> None:
        """校验知识库名称在同一领域空间内唯一（仅约束未删除记录）。

        [jonex] 与 DB 的 partial unique index `uq_kb_info_name_per_space`
        （`deploy/postgres/update/055_knowledge_info_name_unique.sql`）**同口径**：
        `tenant_id + space_id + name + is_deleted = 0`。
        两者职责不同，缺一不可 ——
          * 这一层负责**说人话**：DB 只能给出约束名，给不出「该领域空间内已存在同名知识库」；
          * 索引负责**兜住并发窗口**：两个请求同时 select 都查不到、再同时 INSERT。

        ⚠ 改这里的条件时必须同步改索引的列，否则两层判定分叉：应用层放行、索引拒绝，
        用户看到的是不可读的 IntegrityError；反过来则是两条重名真的并存。
        """
        name = (name or "").strip()
        conditions = [
            KnowledgeInfo.tenant_id == tenant_id,
            KnowledgeInfo.space_id == space_id,
            KnowledgeInfo.name == name,
            KnowledgeInfo.is_deleted == 0,
        ]
        if exclude_id:
            conditions.append(KnowledgeInfo.id != exclude_id)
        result = await session.execute(
            select(KnowledgeInfo.id).where(*conditions).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            raise ResourceConflictError(
                message=translate(
                    "err.kb.name_exists",
                    params={"name": name},
                    fallback=f"该领域空间内已存在同名知识库: {name}",
                )
            )

    async def create(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        # space_id 显式校验（2026-08-19）：不再走 _REQUIRED_FIELDS 通用必填（报错不友好，
        # 只提示「参数验证失败」），service 层给出业务化错误「请先选择所属领域空间」
        space_id = data.get("space_id")
        if not space_id:
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate

            raise InvalidParameterError(
                message=translate(
                    "err.kb.space_required",
                    params={},
                    fallback="请先选择知识库所属的领域空间",
                ),
            )
        # [jonex] 名称统一 strip 后写入：查重按 strip 比对（见 _ensure_name_unique），
        # 写入若保留首尾空格，`"测试"` 与 `"测试 "` 就会作为两个名字并存 ——
        # 应用层看起来"查过重了"，实际还是漏。前端提交前已 trim，此处把口径钉在服务层。
        kb_name = (data["name"] or "").strip()

        # [jonex] 长度上限（2026-10-16）：放在 kb_type 解析之前 —— 长度是**请求自身**的
        # 属性，不该受本部署的 `capability_runtime` / 开关配置影响，也没必要为一个
        # 明显非法的名字去解析"允许建哪类库"。
        _check_text_lengths(kb_name, data.get("description"))

        # [jonex] kb_type 校验：裸 dict 透传无类型校验，非法值会静默降级。
        # 两级、两个错误码，都在 `resolve_creatable_kb_type()` 里（单一判定点）：
        #   ① 值**不认识**（调用方拼错）      → err.kb.invalid_kb_type
        #   ② 值认识但**本部署禁用**（运维配置）→ err.kb.kb_type_disabled
        # 缺省值也走同一道闸门 —— `kb_type` 创建后不可变，不能替用户改选。
        raw_kb_type = resolve_creatable_kb_type(data.get("kb_type"))

        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            # [jonex] 同空间重名校验（2026-10-16）：必须在 INSERT 之前。
            # 放在配额校验**之前**是刻意的 —— 查重是一次廉价的索引查询，
            # 没必要先拿配额 advisory lock 再告诉用户"名字重复了"。
            await self._ensure_name_unique(session, tenant_id, space_id, kb_name)
            # [jonex] 配额：新建知识库前校验单租户 KB 数量（advisory lock 原子化）。
            from .quota_service import QuotaService
            await QuotaService().check_kb_creation(session, tenant_id)
            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                space_id=space_id,
                name=kb_name,
                description=data.get("description"),
                data_source_types=data.get("data_source_types", []),
                status=data.get("status", "synced"),
                owner_id=data.get("owner_id"),
                kb_type=raw_kb_type,  # [jonex]
            )

            # ── 默认数据源：新建 KB 自动创建「文件上传」(file) 数据源 ──
            # 上传链路 _require_file_data_source_id 依赖该数据源存在；
            # 与 KB 创建同事务，KB 创建失败则一并回滚。
            await KnowledgeDataSourceRepository(session).create(
                KnowledgeDataSource(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    knowledge_base_id=obj.id,
                    access_method_id="dam_demo_file",  # 平台共享接入方式目录的「文件上传」条目（006 固定种子 id）
                    access_type="file",
                    name="文件上传",
                    config_json={},
                    sync_mode="manual",
                    status="active",
                )
            )

            # ── [jonex] 默认解析配置自动初始化：五类平台推荐解析配置 ──
            await self._init_default_parser_settings(session, tenant_id, obj.id)

            await session.commit()

        # ── [jonex] llm-wiki KB 创建一条龙（方案 §15，创建事务提交后执行）──
        # ① init_kb：openkb 落盘 KB 目录 + AGENTS.md；jonex_wiki 无 init_kb（首次 ingest 即建）→ no-op + 日志
        # ② 创建默认 LLM-Wiki Schema（DB）——两引擎都照做
        # ③ 立即 apply_schema 投影（AGENTS.md/config.yaml/jonex_schema.json）——两引擎都照做
        # apply 失败不阻断 KB 创建（sync_status=apply_failed，下次编译/保存前补偿）
        if raw_kb_type in LLM_WIKI_TYPES:
            from .llm_wiki_schema_service import LlmWikiSchemaService

            if raw_kb_type == "openkb":
                from .openkb_service import KnowledgeCompilerService
                try:
                    await KnowledgeCompilerService().init_kb(
                        kb_name=obj.id, tenant_id=tenant_id, kb_id=obj.id,
                    )
                except Exception:
                    logger.warning("openkb init_kb 失败 kb=%s（创建不阻断）", obj.id, exc_info=True)
            else:
                logger.info("jonex_wiki KB 无 init_kb，首次 ingest 即建 kb=%s", obj.id)
            try:
                await LlmWikiSchemaService().get_schema(
                    tenant_id, obj.id, auto_create=True,
                )
            except Exception:
                logger.warning("llm-wiki 默认 schema 创建失败 kb=%s（创建不阻断）", obj.id, exc_info=True)

        return obj.to_dict()

    async def _init_default_parser_settings(self, session, tenant_id: str, kb_id: str) -> None:
        """新建 KB 自动初始化默认解析配置（与创建同事务，失败整体回滚）。

        五类平台推荐解析配置（document/txt/image/audio/video），消除解析门槛。
        「文件上传」数据源已在 create() 内联创建（带 access_method_id），此处不再重复。

        容错：某类推荐解析器缺失或非 active 时跳过该类并记 warning，不阻断 KB 创建。
        解析配置不填 prompt_text，因此不触发 atomic-rag prompt 联动（无副作用）。
        """
        from ..repository.parser_setting_repository import KnowledgeParserSettingRepository
        from .parser_setting_service import RECOMMENDED_PARSER_BY_TYPE

        ps_repo = KnowledgeParserSettingRepository(session)

        for parser_type, parser_config_id in RECOMMENDED_PARSER_BY_TYPE.items():
            if not await self._parser_config_active(session, parser_config_id, parser_type):
                logger.warning(
                    "推荐解析器不可用，跳过默认解析配置 kb=%s type=%s config=%s",
                    kb_id, parser_type, parser_config_id,
                )
                continue
            await ps_repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                knowledge_base_id=kb_id,
                parser_type=parser_type,
                parser_config_id=parser_config_id,
                preprocessing_json=[],
                postprocessing_json=[],
                status="active",
            )

    @staticmethod
    async def _parser_config_active(session, parser_config_id: str, parser_type: str) -> bool:
        """校验推荐解析器存在、active 且类目一致；否则返回 False（供调用方容错跳过）。"""
        from sqlalchemy import text

        row = (
            await session.execute(
                text(
                    "SELECT parser_type, status FROM business_domain.parser_configs "
                    "WHERE id = :pid AND is_deleted = 0"
                ),
                {"pid": parser_config_id},
            )
        ).first()
        if row is None or row[1] != "active":
            return False
        actual = str(row[0] or "").strip().lower().replace(" ", "_").replace("/", "_")
        return actual == parser_type

    async def get(self, kb_id: str, tenant_id: str, user_id: str | None = None,
                  with_stats: bool = False) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            from ..models.space import Space

            repo = KnowledgeInfoRepository(session)
            obj = await repo.get_required(kb_id, tenant_id)

            space_name = ""
            space_row = await session.execute(
                select(Space.name).where(
                    Space.id == obj.space_id,
                    Space.tenant_id == tenant_id,
                    Space.is_deleted == 0,
                )
            )
            result = space_row.scalar()
            if result:
                space_name = result

            # document_count 按文档表实时统计
            from ..repository.document_repository import KnowledgeDocumentRepository
            doc_count_map = await KnowledgeDocumentRepository(session).count_by_knowledge_bases(
                tenant_id, [obj.id]
            )
            data = obj.to_dict(space_name=space_name)
            data["document_count"] = doc_count_map.get(obj.id, 0)
            # 管理权限（空间管理者、租户管理员，或 kb_manager 授权）——详情页权限 tab 只读态依据
            from .space_permission_service import get_write_space_ids
            from .kb_permission_service import get_kb_grant_role

            write_ids = await get_write_space_ids(tenant_id, user_id)
            grant_role = await get_kb_grant_role(tenant_id, kb_id, user_id)

            # [jonex] 权限重构 B4（方案 §5 Gap 8 / 执行文档 §5.4）：
            # KB 详情与列表同口径 —— 非空间管理者且无 KB 授权 → **404 而非 403**。
            # 用 404 是为了防探测：403 会告诉调用方「这个 id 存在」，与空间侧
            # `require_space_visible` 的口径一致。
            #
            # 复用上面已经查好的 write_ids / grant_role，不额外查库。
            # 判据不含「空间 member」：他能检索到这个 KB 的内容、能点开原文（D8），
            # 但**不能浏览这个知识库**。这是本次收紧的核心语义差。
            if write_ids is not None and obj.space_id not in write_ids and grant_role is None:
                raise ResourceNotFoundError(
                    message=translate(
                        "err.kb.not_found",
                        params={"kb_id": kb_id},
                        fallback=f"知识库不存在: {kb_id}",
                    )
                )

            data["can_manage_permissions"] = (
                write_ids is None or obj.space_id in write_ids or grant_role == KB_MANAGER
            )
            # KB 写权限（空间管理者/租户管理员，或 kb_manager 授权）——详情页各 tab 写按钮依据
            # [jonex] B3：原判据是 `in ("kb_manager", "editor")`。D1 删掉 editor 之后
            # 「可写」的授权侧判据与 can_manage_permissions 同源 —— member 只读。
            data["can_write"] = (
                (write_ids is None or obj.space_id in write_ids)
                or grant_role == KB_MANAGER
            )

        # [jonex] 首屏提速：详情页（DomainKnowledgeBlank）只靠 kb_type + 基本信息判断渲染面板。
        # 实体/关系统计按需加载：llm-wiki 库的图谱统计是跨容器全图扫描（jonex_wiki
        # list_graph 实测 ~2.6s），首屏（with_stats=false）跳过；lightrag 库的 Neo4j 统计
        # 快、且页头「本体/关系实例」标签依赖它，首屏保留（跳过了会误显示 0）。设置页
        # 带 with_stats=true 拉全量。
        if not with_stats and data.get("kb_type") in LLM_WIKI_TYPES:
            return data

        # [jonex] llm-wiki 管线：本体统计走 Wiki（Neo4j 里恒空，读了会误报 0）。
        # 三分支显式派发：jonex_wiki 走 JonexWikiService（透传 list_graph），openkb 走 KnowledgeCompilerService。
        if data.get("kb_type") in LLM_WIKI_TYPES:
            if data.get("kb_type") == "jonex_wiki":
                from .jonex_wiki_service import JonexWikiService
                compiler = JonexWikiService()
            else:
                from .openkb_service import KnowledgeCompilerService
                compiler = KnowledgeCompilerService()
            try:
                graph = await compiler.get_graph(
                    kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
                ) or {}
                data["entity_count"] = len(graph.get("entities") or [])
                data["relation_count"] = len(graph.get("relationships") or [])
                data["ontology_degraded"] = False
            except Exception:  # noqa: BLE001
                data["entity_count"] = 0
                data["relation_count"] = 0
                data["ontology_degraded"] = True
            return data

        # 集成本体 kb 维度统计：本体实例数 / 关系数 / 降级标记。
        # get_kb_statistics 自带 Neo4j 优雅降级（不可用时计数为 0 + degraded=True）；
        # 这里再包一层防御，确保任何统计异常都不阻塞知识库详情主体返回。
        from .ontology_query_service import OntologyQueryService
        try:
            stats = await OntologyQueryService().get_kb_statistics(
                tenant_id, {"knowledge_base_id": kb_id}
            )
            data["entity_count"] = stats.get("ontology_instance_count", 0)
            data["relation_count"] = stats.get("ontology_relation_count", 0)
            data["ontology_degraded"] = stats.get("ontology_degraded", False)
        except Exception:  # noqa: BLE001
            data["entity_count"] = 0
            data["relation_count"] = 0
            data["ontology_degraded"] = True

        return data

    async def list(self, tenant_id: str, space_id: str = None, status: str = None,
                   keyword: str = None, offset: int = 0, limit: int = 20,
                   user_id: str | None = None, scope: str = "manage") -> dict:
        tenant_id = require_tenant(tenant_id)
        # [jonex] 权限重构 B4（方案 §5 Gap 8 / 执行文档 §5.2）：
        # KB 列表可见范围 = **我管的空间下全部 KB ∪ 我被授权的 KB**。
        #
        # ⚠️ `scope="manage"`（默认）**故意不用** `get_visible_space_ids` —— 那是
        #    「检索 / 领域服务可见」口径。
        #    两个口径的差别只在**空间 member**：
        #      · 检索口径含他所在的整个空间 → 他能检索到空间下全部 KB 的内容，
        #        并且能点开引用看原文（方案 D8 明确「能打开，只是看不到知识库」）；
        #      · 列表口径不含 → 他在领域知识库页看不到这些 KB。
        #    **不要把这两个口径合并成一个函数、也不要"顺手统一"** —— 合并即回归。
        #    检索侧在 `capability.py::_filter_search_kbs`，那段一行都没动。
        #
        # [jonex] `scope="search"` 是**新增的显式分支**，供检索面（搜索页 / 引用富化）
        #    按检索口径取 KB：可见空间 ∪ 授权 KB。默认值不变，管理面行为完全不受影响。
        #    起因：搜索页的「空间」维度原先只能走列表口径，纯空间 member 拿到 0 个 KB，
        #    检索范围解析为空 → `handleSearch` 直接 return、请求发不出去。
        #
        # 副作用（已在方案 §6 决策不补发授权，需上线公告）：存量空间 member 的
        # KB 列表上线后会变空，直到有人把他加成 KB 成员。
        from .space_permission_service import get_write_space_ids

        async with get_db_session() as session:
            from ..models.space import Space

            repo = KnowledgeInfoRepository(session)
            conditions = []
            if space_id:
                conditions.append(KnowledgeInfo.space_id == space_id)
            # None = 不过滤（租户管理员 / 平台管理员 / 无 user_id / anonymous 内部链路）。
            # 原实现这里查两次（get_visible_space_ids + get_write_space_ids），
            # 收紧后只需可管空间一次 → **少一次 SQL**。
            manage_ids = await get_write_space_ids(tenant_id, user_id)

            granted: list[str] = []
            manager_ids: list[str] = []
            if manage_ids is not None:
                # 授权 KB 叠加（manage_ids None = 租户管理员/内部链路：不加条件、也不查授权
                # ——否则租户管理员白付一次查询，与 Task 6「仅被过滤时补查」的对称性冲突）
                from .kb_permission_service import get_granted_kb_ids

                granted = await get_granted_kb_ids(tenant_id, user_id)
                # kb_manager 授权集合（KB 写权限与人员管理权限的共同来源，批量一次查询）
                # [jonex] B3（N4）：原先分别查 editor 集合与 manager 集合，
                # D1 收为两级后两者等价 —— 合并成一次查询，少一次 DB 往返。
                # manage_ids is None 时两个布尔恒 True，这次查询纯属浪费，一并跳过。
                from .kb_permission_service import get_granted_manager_kb_ids
                manager_ids = await get_granted_manager_kb_ids(tenant_id, user_id)

            # [jonex] 过滤条件按 scope 显式二选一（默认 manage，行为完全不变）：
            #   manage = 我管的空间 ∪ 我授权的 KB（领域知识库页 / 管理面）
            #   search = 可见空间   ∪ 我授权的 KB（搜索页 / 引用富化等检索面）
            # 两者差别只在**空间 member**：他能检索到空间下全部 KB，却在知识库列表里看不到。
            # ⚠️ 不要合并、也不要「顺手统一」—— 见上面长注释与方案 §5 Gap 8，合并即回归。
            # 注：granted / manager_ids 在上方照常计算，行内的 grant_shared / can_write /
            #     can_manage_permissions 三个字段因此不受本分支影响。
            if scope == "search":
                from .space_permission_service import get_visible_space_ids

                visible = await get_visible_space_ids(tenant_id, user_id)
                if visible is not None:      # None = 租户管理员/内部链路 → 不过滤
                    conditions.append(or_(
                        KnowledgeInfo.space_id.in_(visible),
                        KnowledgeInfo.id.in_(granted),
                    ))
            elif manage_ids is not None:
                # in_() 要 list；manage_ids 是 set。两边都空时整个 or_ 恒 false → 空列表，
                # 这正是空间 member 无 KB 授权时期望的结果（total == 0）。
                conditions.append(or_(
                    KnowledgeInfo.space_id.in_(list(manage_ids)),
                    KnowledgeInfo.id.in_(granted),
                ))
            if status:
                conditions.append(KnowledgeInfo.status == status)

            items = await repo.list_all(tenant_id, offset, limit, extra_conditions=conditions)
            total = await repo.count(tenant_id, extra_conditions=conditions)

            # document_count 按文档表实时统计（冗余 document_count 列会漂移，不作准）
            from ..repository.document_repository import KnowledgeDocumentRepository
            doc_count_map = await KnowledgeDocumentRepository(session).count_by_knowledge_bases(
                tenant_id, [o.id for o in items]
            )

            # Batch fetch space names
            space_ids = list({o.space_id for o in items})
            space_name_map: dict[str, str] = {}
            if space_ids:
                space_rows = await session.execute(
                    select(Space.id, Space.name).where(
                        Space.id.in_(space_ids),
                        Space.tenant_id == tenant_id,
                        Space.is_deleted == 0,
                    )
                )
                space_name_map = {row[0]: row[1] for row in space_rows.all()}

            # set 在循环外 hoist（granted_set/manager_set 每请求建一次，勿逐行重建）
            granted_set = set(granted)
            manager_set = set(manager_ids)
            result_items = []
            for o in items:
                d = o.to_dict(space_name=space_name_map.get(o.space_id, ""))
                d["document_count"] = doc_count_map.get(o.id, 0)
                # KB 授权共享标识：这条 KB 只因为「我被单独授权」才出现在列表里
                # （即不在我可管的空间内）。manage_ids None（租户管理员）恒 False。
                # [jonex] B4：判据从 visible（空间可见）换成 manage_ids（空间可管），
                # 与上面的过滤条件保持同一口径 —— 否则空间 member 靠授权看到的 KB
                # 会因为「空间可见」而不被标记成共享，标记语义与列表语义脱节。
                d["grant_shared"] = (
                    manage_ids is not None
                    and o.space_id not in manage_ids
                    and o.id in granted_set
                )
                # 管理权限（空间管理者、租户管理员，或 kb_manager 授权）
                d["can_manage_permissions"] = (
                    manage_ids is None or o.space_id in manage_ids or o.id in manager_set
                )
                # KB 写权限 —— 列表页编辑/操作按钮依据。
                # [jonex] B3：两级模型下「可写」与「可管人员」同源（都是 kb_manager），
                # 所以这两个字段的授权侧判据变成同一个 manager_set。
                # 保留两个字段是因为**空间侧**仍有区别：write_ids 给 can_write，
                # 而 can_manage_permissions 语义上是「能改这个 KB 的人员名单」。
                d["can_write"] = (
                    (manage_ids is None or o.space_id in manage_ids) or o.id in manager_set
                )
                result_items.append(d)
            if keyword:
                kw = keyword.lower()
                result_items = [
                    i for i in result_items
                    if kw in (i.get("name") or "").lower()
                    or kw in (i.get("description") or "").lower()
                ]

            return {
                "items": result_items,
                "total": len(result_items) if keyword else total,
                "offset": offset, "limit": limit,
            }

    async def update(self, kb_id: str, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        # [jonex] kb_type 创建后不可变：改类型意味着旧数据全部失效（openkb→lightrag
        # 要清空 Wiki 重推 LightRAG；lightrag→openkb 要清空 Neo4j 重编译），
        # 没有安全的原地切换路径。如需更换请新建知识库。
        # 连旧名 pipeline_type 一起拦：防止调用方误以为旧名仍有效
        if "kb_type" in data or "pipeline_type" in data:
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate
            raise InvalidParameterError(
                message=translate(
                    "err.kb.kb_type_immutable",
                    fallback="知识库类型创建后不可修改，如需更换请新建知识库",
                ),
            )
        # KB 授权路径（execute 兜底打标）：editor 禁改 space_id/owner_id（防搬迁提权）；
        # 空间成员路径行为不变。
        # [jonex] 白名单与值校验都放在**碰 DB 之前**（与上面的 kb_type 闸门同一条纪律：
        # 非法值不去读库）。`ingest_backend`（2026-10-09 库级派单值）同样是提权面 ——
        # 它决定这个库的文档派去哪个后端 —— 所以只在 admin 名单里，editor 不可改。
        kb_granted = data.pop("_kb_granted", False)
        updatable = {"name", "description", "data_source_types", "status", "space_id", "owner_id",
                     "ingest_backend"}
        if kb_granted:
            updatable = {"name", "description", "data_source_types", "status"}
        # [jonex] 库级派单值取值校验：None = 撤销（回到跟随服务端开关），其余必须是
        # 本代码认识的值。只对「真要落的字段」校验 —— editor 的越权值照其它字段的
        # 口径静默丢弃，不在这里炸。
        if "ingest_backend" in updatable and data.get("ingest_backend") is not None:
            from . import ingest_backend
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate
            value = data["ingest_backend"]
            if not isinstance(value, str) or value not in ingest_backend.VALID_BACKENDS:
                raise InvalidParameterError(
                    message=translate(
                        "err.kb.ingest_backend_invalid",
                        params={
                            "value": value,
                            "valid": ", ".join(ingest_backend.VALID_BACKENDS),
                        },
                        fallback=(
                            f"入库后端取值不合法：{value}，可选值："
                            f"{', '.join(ingest_backend.VALID_BACKENDS)}"
                        ),
                    ),
                )
        # [jonex] 长度上限（2026-10-16）：与上面的 kb_type / ingest_backend 闸门同一纪律
        # —— 非法值不去读库。判据用 `updatable` 而不是直接看 data：白名单是"这个字段到底
        # 受理不受理"的唯一口径，调用方身份（kb_granted）变了这里自动跟随，不会出现
        # "字段被静默丢弃、却先为它报了个长度错"的错位。
        _check_text_lengths(
            name=data.get("name") if "name" in updatable else None,
            description=data.get("description") if "description" in updatable else None,
        )
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            # [jonex] 接住返回值：搬迁校验要读 obj.space_id / obj.name 作为"目标值缺省来源"。
            obj = await repo.get_required(kb_id, tenant_id)
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
            # [jonex] `ingest_backend` 的显式 None 是**撤销**（回到跟随服务端开关）；
            # 其余字段的 None 仍是「不更新」—— 不特判的话这个设置只能设不能撤，
            # 撤销只能去改库，等于把它做成单向门。
            if "ingest_backend" in updatable and data.get("ingest_backend", "missing") is None:
                values["ingest_backend"] = None
            # [jonex] 同空间重名校验（2026-10-16）：只在真的动 name / space_id 时查。
            #
            # ⚠ **必须用"目标 space_id"**：`space_id` 在本文件的 updatable 白名单里
            # （知识库可跨空间搬迁），而领域服务那边没有这一项 —— 照抄
            # `DomainServiceService` 的 `obj.space_id` 写法会漏掉
            # 「搬进一个已有同名知识库的空间」这条路径：查的是旧空间，查不到冲突，
            # 放行，目标空间出现重名。前端编辑知识库时会真的带上 space_id
            # （`DomainKnowledge/index.tsx` 的 data 对象 create/update 共用），
            # 所以这是活路径。
            if "name" in values or "space_id" in values:
                target_space_id = values.get("space_id", obj.space_id)
                target_name = (values.get("name", obj.name) or "").strip()
                if "name" in values:
                    # 与 create 同口径：查重按 strip 比对，写入也用 strip 后的值
                    values["name"] = target_name
                await self._ensure_name_unique(
                    session, tenant_id, target_space_id, target_name, exclude_id=kb_id,
                )
            if values:
                obj = await repo.update(kb_id, tenant_id, **values)
                await session.commit()
                return obj.to_dict()
            obj = await repo.get_required(kb_id, tenant_id)
            return obj.to_dict()

    async def delete(self, kb_id: str, tenant_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            await repo.get_required(kb_id, tenant_id)

            # 删库残留清理：文档行/配置/派生关联/对象存储。
            # 检索索引不在这里清（归文档删除链路与任务看门狗）。
            residue = await cleanup_kb_residue(session, tenant_id, kb_id)

            await repo.delete_soft(kb_id, tenant_id)

            # 级联：硬删该 KB 的授权记录
            await session.execute(
                text(
                    "DELETE FROM knowledge_base.kb_permissions "
                    "WHERE kb_id=:kb AND tenant_id=:t AND is_deleted=0"
                ),
                {"kb": kb_id, "t": tenant_id},
            )

            # 级联：同事务内软删除该 KB 的所有同义词组
            from ..repository.ontology_synonym_repository import OntologySynonymRepository
            syn_repo = OntologySynonymRepository(session)
            for group in await syn_repo.list_all_by_kb(tenant_id, kb_id):
                await syn_repo.delete_soft(group)

            await session.commit()

        # 对象存储放在事务提交后 best-effort 清理：失败时文档行仍保留
        # `storage_key`（软删不清列），可据此重试；不能因为一个对象失败就回滚删库。
        from jonex_core.common.object_storage import (
            get_object_storage,
            get_object_storage_for,
        )

        storage_failed = 0
        for item in residue.get("storage", []):
            key = item.get("key")
            if not key:
                continue
            backend = item.get("backend")
            try:
                storage = get_object_storage_for(backend) if backend else get_object_storage()
                await storage.delete(key)
            except Exception:
                storage_failed += 1
                logger.warning(
                    "Failed to delete storage object %s for deleted KB %s",
                    key, kb_id, exc_info=True,
                )

        summary = {
            "documents": residue.get("documents", 0),
            "document_tags": residue.get("document_tags", 0),
            "soft_deleted": residue.get("soft_deleted", {}),
            "deleted": residue.get("deleted", {}),
            "storage_failed": storage_failed,
        }
        return {"deleted": True, "cleanup": summary}

    async def get_kb_space_id(self, kb_id: str, tenant_id: str) -> str | None:
        """KB → space_id（权限管理判定用）；KB 不存在返回 None。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            row = (await session.execute(
                text(
                    "SELECT space_id FROM knowledge_base.knowledge_info "
                    "WHERE id=:kb AND tenant_id=:t AND is_deleted=0"
                ),
                {"kb": kb_id, "t": tenant_id},
            )).first()
        return row[0] if row else None

    async def get_permissions(self, kb_id: str, tenant_id: str,
                              user_id: str | None = None) -> list:
        """KB 授权成员列表（display_name 后端 join；KB 可读者可调，execute 层兜底）。"""
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT kp.user_id, kp.role, u.display_name, kp.created_at
                      FROM knowledge_base.kb_permissions kp
                      LEFT JOIN platform.users u
                        ON u.id::text = kp.user_id
                       AND u.is_deleted = 0
                     WHERE kp.kb_id = :kb_id
                       AND kp.tenant_id = :tenant_id
                       AND kp.is_deleted = 0
                """),
                {"kb_id": kb_id, "tenant_id": tenant_id},
            )
            return [
                {
                    "id": row[0],
                    "user_id": row[0],
                    "role": row[1],
                    "display_name": row[2],
                    "created_at": row[3].isoformat() if row[3] else None,
                }
                for row in result.all()
            ]

    async def get_permission_candidates(self, kb_id: str, tenant_id: str,
                                        user_id: str | None = None) -> list:
        """KB「添加成员」候选用户 = 父空间成员池 - 已有 KB 成员 - 操作者本人。

        候选池取父空间成员而非全租户用户 —— require_parent_membership 要求
        被授权人必须已在父空间成员池内。鉴权与 set_permissions 写入口径一致：
        空间管理者 / 租户管理员 / kb_manager；invoke 链路在 capability 层已由
        _KB_MANAGE_ACTIONS 判定，此处是 REST 直连兜底。
        """
        tenant_id = require_tenant(tenant_id)
        space_id = await self.get_kb_space_id(kb_id, tenant_id)
        if space_id is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.kb_permission.not_found",
                    params={"kb_id": kb_id},
                    fallback=f"知识库 {kb_id} 不存在或无权访问",
                )
            )
        from .space_permission_service import SPACE_MANAGER, has_space_role, is_tenant_admin
        from .kb_permission_service import get_kb_grant_role
        if not (
            await has_space_role(tenant_id, space_id, user_id, SPACE_MANAGER)
            or await is_tenant_admin(tenant_id, user_id)
            or await get_kb_grant_role(tenant_id, kb_id, user_id) == KB_MANAGER
        ):
            raise PermissionDeniedError(
                message=translate(
                    "err.kb_permission.manage_required",
                    params={"kb_id": kb_id},
                    fallback=f"仅空间管理者或租户管理员可管理知识库权限: {kb_id}",
                )
            )
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT sp.user_id, u.username, u.display_name, u.email
                      FROM knowledge_base.space_permissions sp
                      LEFT JOIN platform.users u
                        ON u.id::text = sp.user_id
                       AND u.is_deleted = 0
                     WHERE sp.space_id = :space_id
                       AND sp.tenant_id = :tenant_id
                       AND sp.is_deleted = 0
                       AND sp.user_id <> :user_id
                       AND NOT EXISTS (
                           SELECT 1
                             FROM knowledge_base.kb_permissions kp
                            WHERE kp.kb_id = :kb_id
                              AND kp.tenant_id = :tenant_id
                              AND kp.user_id = sp.user_id
                              AND kp.is_deleted = 0
                       )
                     ORDER BY sp.user_id
                """),
                {"space_id": space_id, "tenant_id": tenant_id, "kb_id": kb_id,
                 "user_id": user_id or ""},
            )
            return [
                {
                    "user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "email": row[3],
                }
                for row in result.all()
            ]

    async def set_permissions(self, kb_id: str, tenant_id: str, permissions: list,
                              user_id: str | None = None) -> bool:
        """设置 KB 授权列表。管理员 = 空间 owner/manager 或租户管理员（get_write_space_ids）。"""
        tenant_id = require_tenant(tenant_id)
        from .space_permission_service import get_write_space_ids

        space_id = await self.get_kb_space_id(kb_id, tenant_id)
        if space_id is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.kb_permission.not_found",
                    params={"kb_id": kb_id},
                    fallback=f"知识库 {kb_id} 不存在或无权访问",
                )
            )
        # [jonex] 权限重构 B2（E3）：**撤掉了「空间创建者已拥有全部权限，禁止单独授权」拦截**。
        #
        # 原实现按 `spaces.owner_id` 判定。D2 取消 owner 概念后这个判据同时**过宽又过窄**：
        #   · 过窄：真正「已拥有全部权限」的是空间**管理者**（向下穿透管所有 KB），
        #           而管理者不一定是创建人 —— 别的 space_manager 不被拦；
        #   · 过宽：创建人若已被移除管理者身份（空间转交），他并没有权限，却仍被拦住。
        #
        # 且本方法与 space_service.set_permissions 一样是**全量替换**语义：
        # 某人先是 KB 成员、后被提升为空间管理者，KB 权限页原样提交就会保存失败。
        #
        # 「谁可以被授权」这件事由 B5 的成员池约束（require_parent_membership）统一承担，
        # 那是按**当前身份**判定的，不依赖「谁是创建人」这个会过期的事实。
        write_ids = await get_write_space_ids(tenant_id, user_id)
        if write_ids is not None and space_id not in write_ids:
            from .kb_permission_service import get_kb_grant_role
            if await get_kb_grant_role(tenant_id, kb_id, user_id) != KB_MANAGER:
                raise PermissionDeniedError(
                    message=translate(
                        "err.kb_permission.manage_required",
                        params={"kb_id": kb_id},
                        fallback=f"仅空间管理者或租户管理员可管理知识库权限: {kb_id}",
                    )
                )
        # role 白名单（两级：kb_manager / member）+ user_id 数字校验
        # [jonex] B3：拒绝旧取值 editor/viewer。前端若未同批更新会在这里 400 ——
        # 这是刻意的，比静默接受旧值然后行为不一致要好。
        for perm in permissions:
            if perm.get("role") not in _KB_ROLE_RANK:
                raise InvalidParameterError(
                    message=translate(
                        "err.kb_permission.invalid_role",
                        params={"role": str(perm.get("role"))},
                        fallback=(
                            f"非法角色: {perm.get('role')}"
                            f"（仅支持 {KB_MANAGER} / {KB_MEMBER}）"
                        ),
                    )
                )
            if not str(perm.get("user_id", "")).isdigit():
                raise InvalidParameterError(
                    message=translate(
                        "err.kb_permission.invalid_user_id",
                        params={"user_id": str(perm.get("user_id", ""))},
                        fallback=f"非法用户 ID: {perm.get('user_id', '')}（必须为数字）",
                    )
                )

        # [jonex] 权限重构 B5（方案 §3.5 / 执行文档 §6.3 步骤 1）：成员池约束。
        # 顺序重要 —— **在 role 白名单校验之后、写库之前**：先确认取值合法，再确认对象合法，
        # 两类错误信息才不会互相掩盖（先报「非法角色」还是先报「不在成员池」是确定的）。
        # 被授权人必须已在**本 KB 所属空间**的成员池内，否则 400。
        from .membership_service import require_parent_membership
        await require_parent_membership(
            tenant_id, "space", space_id,
            [str(p["user_id"]) for p in permissions],
        )

        from ..models.kb_permission import KbPermission

        async with get_db_session() as session:
            existing = await session.execute(
                select(KbPermission).where(
                    KbPermission.kb_id == kb_id,
                    KbPermission.tenant_id == tenant_id,
                    KbPermission.is_deleted == 0,
                )
            )
            for kp in existing.scalars().all():
                await session.delete(kp)
            # 关键：先 flush 让 DELETE 落库再插入（UoW 默认 INSERT 先于 DELETE，撞唯一索引）
            await session.flush()

            # [jonex] B3：取值与优先级统一由 kb_permission_service._KB_ROLE_RANK 定义，
            # 不再在这里另写一份 —— 原先本地这份 {viewer:0, editor:1, kb_manager:2}
            # 与判定服务里的取值集合是两处独立定义，加减层级时必然漏改一处。
            _ROLE_RANK = _KB_ROLE_RANK
            deduped: dict[str, str] = {}
            for perm in permissions:
                uid = str(perm["user_id"])
                role = perm["role"]
                if uid not in deduped or _ROLE_RANK[role] > _ROLE_RANK[deduped[uid]]:
                    deduped[uid] = role
            for uid, role in deduped.items():
                session.add(KbPermission(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    user_id=uid,
                    role=role,
                ))
            await session.commit()
            schedule_emit({
                "tenant_id": tenant_id,
                "log_type": "OPERATION",
                "action": "set_kb_permissions",
                "outcome": "SUCCESS",
                "service_name": "knowledge_base",
                "resource": ResourceType.KNOWLEDGE_INFO.value,  # audit_enums.py:52，已核实存在
                "resource_id": kb_id,
            })
            return True