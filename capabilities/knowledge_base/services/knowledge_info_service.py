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
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..models.data_source import KnowledgeDataSource
from ..models.knowledge_info import KnowledgeInfo
from ..repository.data_source_repository import KnowledgeDataSourceRepository
from ..repository.domain_service_repository import (
    DomainServiceRepository,
    ServiceKnowledgeBaseRepository,
)
from ..repository.knowledge_info_repository import KnowledgeInfoRepository


class KnowledgeInfoService:
    """知识库信息 CRUD"""

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
        kb_name = data["name"]

        # [jonex] kb_type 白名单校验：裸 dict 透传无类型校验，非法值会静默降级
        from .kb_type_service import DEFAULT_KB_TYPE, VALID_KB_TYPES
        raw_kb_type = (data.get("kb_type") or "").strip() or DEFAULT_KB_TYPE
        if raw_kb_type not in VALID_KB_TYPES:
            from jonex_core.common.exceptions import InvalidParameterError
            from jonex_core.common.i18n import translate
            raise InvalidParameterError(
                message=translate(
                    "err.kb.invalid_kb_type",
                    params={"value": raw_kb_type, "valid": ", ".join(sorted(VALID_KB_TYPES))},
                    fallback=f"知识库类型不合法：{raw_kb_type}，可选值：{', '.join(sorted(VALID_KB_TYPES))}",
                ),
            )

        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
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

            # ── 自动绑定：确保该 space 下的知识库对知识检索可见 ──
            # 检查 space 下是否已有 DomainService，有则绑定，无则创建默认服务再绑定。
            from ..models.domain_service import DomainService

            ds_repo = DomainServiceRepository(session)
            svc_kb_repo = ServiceKnowledgeBaseRepository(session)

            existing = await ds_repo.list_all(
                tenant_id, 0, 1,
                extra_conditions=[DomainService.space_id == space_id],
            )

            if existing:
                service = existing[0]
            else:
                service = await ds_repo.create(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    space_id=space_id,
                    name=kb_name,
                    description=f"Auto-created for knowledge base: {kb_name}",
                )

            await svc_kb_repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                service_id=service.id,
                kb_id=obj.id,
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

        # ── [jonex] openkb KB 创建一条龙（方案 §15，创建事务提交后执行）──
        # ① init_kb（幂等：KB 目录 + AGENTS.md 内置模板落盘）
        # ② 创建默认 LLM-Wiki Schema（DB）
        # ③ 立即 apply_schema 投影（AGENTS.md/config.yaml/jonex_schema.json）
        # apply 失败不阻断 KB 创建（sync_status=apply_failed，下次编译/保存前补偿）
        if raw_kb_type == "openkb":
            from .llm_wiki_schema_service import LlmWikiSchemaService
            from .openkb_service import KnowledgeCompilerService

            try:
                await KnowledgeCompilerService().init_kb(
                    kb_name=obj.id, tenant_id=tenant_id, kb_id=obj.id,
                )
            except Exception:
                logger.warning("openkb init_kb 失败 kb=%s（创建不阻断）", obj.id, exc_info=True)
            try:
                await LlmWikiSchemaService().get_schema(
                    tenant_id, obj.id, auto_create=True,
                )
            except Exception:
                logger.warning("openkb 默认 schema 创建失败 kb=%s（创建不阻断）", obj.id, exc_info=True)

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

    async def get(self, kb_id: str, tenant_id: str, user_id: str | None = None) -> dict:
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
            # 管理权限（空间 owner/manager 或租户管理员）——详情页权限 tab 只读态依据
            from .space_permission_service import get_write_space_ids

            write_ids = await get_write_space_ids(tenant_id, user_id)
            data["can_manage_permissions"] = write_ids is None or obj.space_id in write_ids
            # KB 写权限（空间 owner/manager/租户管理员 或 授权 editor）——详情页各 tab 写按钮依据
            from .kb_permission_service import get_kb_grant_role

            grant_role = await get_kb_grant_role(tenant_id, kb_id, user_id)
            data["can_write"] = (
                (write_ids is None or obj.space_id in write_ids) or grant_role == "editor"
            )

        # [jonex] openkb 管线：本体统计走 OpenKB Wiki（Neo4j 里恒空，读了会误报 0）
        if data.get("kb_type") == "openkb":
            from .openkb_service import KnowledgeCompilerService
            try:
                graph = await KnowledgeCompilerService().get_graph(
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
                   user_id: str | None = None) -> dict:
        tenant_id = require_tenant(tenant_id)
        from .space_permission_service import get_visible_space_ids

        async with get_db_session() as session:
            from ..models.space import Space

            repo = KnowledgeInfoRepository(session)
            conditions = []
            if space_id:
                conditions.append(KnowledgeInfo.space_id == space_id)
            # [jonex] 空间隔离：不传 space_id 的租户级列表也只返回可见空间的 KB。
            # get_visible_space_ids 返回 None = 不过滤（service:write / 内部链路）
            visible = await get_visible_space_ids(tenant_id, user_id)
            from .space_permission_service import get_write_space_ids

            granted: list[str] = []
            if visible is not None:
                # 授权 KB 叠加（visible None = 租户管理员/内部链路：不加条件、也不查授权
                # ——否则租户管理员白付一次查询，与 Task 6「仅被过滤时补查」的对称性冲突）
                from .kb_permission_service import get_granted_kb_ids

                granted = await get_granted_kb_ids(tenant_id, user_id)
                conditions.append(or_(
                    KnowledgeInfo.space_id.in_(visible),
                    KnowledgeInfo.id.in_(granted),
                ))
            write_ids = await get_write_space_ids(tenant_id, user_id)
            # editor 授权集合（KB 写权限的额外来源，批量一次查询）
            from .kb_permission_service import get_granted_editor_kb_ids

            editor_ids = await get_granted_editor_kb_ids(tenant_id, user_id)
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

            # set 在循环外 hoist（visible_set/granted_set/editor_set 每请求建一次，勿逐行重建）
            visible_set = set(visible) if visible is not None else None
            granted_set = set(granted)
            editor_set = set(editor_ids)
            result_items = []
            for o in items:
                d = o.to_dict(space_name=space_name_map.get(o.space_id, ""))
                d["document_count"] = doc_count_map.get(o.id, 0)
                # KB 授权共享标识（仅真正靠授权才看到的 KB；visible None 恒 False）
                d["grant_shared"] = (
                    visible is not None
                    and o.space_id not in visible_set
                    and o.id in granted_set
                )
                # 管理权限（空间 owner/manager 或租户管理员）
                d["can_manage_permissions"] = write_ids is None or o.space_id in write_ids
                # KB 写权限（额外含授权 editor）——列表页编辑/操作按钮依据
                d["can_write"] = (
                    (write_ids is None or o.space_id in write_ids) or o.id in editor_set
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
        async with get_db_session() as session:
            repo = KnowledgeInfoRepository(session)
            await repo.get_required(kb_id, tenant_id)
            # KB 授权路径（execute 兜底打标）：editor 禁改 space_id/owner_id（防搬迁提权）；
            # 空间成员路径行为不变
            kb_granted = data.pop("_kb_granted", False)
            updatable = {"name", "description", "data_source_types", "status", "space_id", "owner_id"}
            if kb_granted:
                updatable = {"name", "description", "data_source_types", "status"}
            values = {k: v for k, v in data.items() if k in updatable and v is not None}
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
            return {"deleted": True}

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
        write_ids = await get_write_space_ids(tenant_id, user_id)
        if write_ids is not None and space_id not in write_ids:
            raise PermissionDeniedError(
                message=translate(
                    "err.kb_permission.manage_required",
                    params={"kb_id": kb_id},
                    fallback=f"仅空间 owner/manager 或租户管理员可管理知识库权限: {kb_id}",
                )
            )
        # role 白名单 {editor, viewer} + user_id 数字校验
        for perm in permissions:
            if perm.get("role") not in ("editor", "viewer"):
                raise InvalidParameterError(
                    message=translate(
                        "err.kb_permission.invalid_role",
                        params={"role": str(perm.get("role"))},
                        fallback=f"非法角色: {perm.get('role')}（仅支持 editor / viewer）",
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

            deduped: dict[str, str] = {}
            for perm in permissions:
                uid = str(perm["user_id"])
                role = perm["role"]
                if uid not in deduped or (role == "editor" and deduped[uid] == "viewer"):
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