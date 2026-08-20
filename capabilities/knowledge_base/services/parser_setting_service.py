#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB 级解析引擎设置服务。"""
import logging
import uuid
from typing import Any

from sqlalchemy import text

from jonex_core.capability.atomic.rag.client import get_rag_client
from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository.parser_setting_repository import KnowledgeParserSettingRepository
from .parser_prompt_map import precheck_prompt_template, prompt_target

logger = logging.getLogger(__name__)


# 解析器类目的展示排序（仅用于列表排序，非枚举约束；未列出的类目排在最后按字母序）
PARSER_TYPE_ORDER = ["document", "txt", "image", "audio", "video", "web", "cad"]

# parser_type -> 平台推荐解析器 parser_config_id（新建 KB 自动初始化时关联）。
# 与 business_domain.parser_configs 种子数据（006_seed_data.sql）保持一致；
# video 选 video_full_pipeline（VLM，系统默认管线），非 video_mps（可选增强）。
RECOMMENDED_PARSER_BY_TYPE: dict[str, str] = {
    "document": "document_parse",
    "txt": "text_parse",
    "image": "image_parse",
    "audio": "audio_transcribe",
    "video": "video_full_pipeline",
}


class ParserSettingService:
    """知识库解析引擎设置 CRUD（按 parser_type 类目组织）。"""

    async def list_settings(self, tenant_id: str, knowledge_base_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeParserSettingRepository(session)
            items = await repo.list_by_kb(tenant_id, knowledge_base_id)
            parser_map = await self._load_parser_map(session, tenant_id)
            enriched = [self._enrich(item.to_dict(), parser_map) for item in items]
            enriched.sort(key=lambda row: self._sort_index(row["parser_type"]))
            return {"items": enriched, "total": len(enriched), "offset": 0, "limit": 100}

    async def create_setting(self, tenant_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        knowledge_base_id = self._required(data, "knowledge_base_id")
        parser_type = self._normalize(self._required(data, "parser_type"))

        async with get_db_session() as session:
            repo = KnowledgeParserSettingRepository(session)
            existing = await repo.get_by_kb_parser_type(tenant_id, knowledge_base_id, parser_type)
            if existing is not None:
                raise InvalidParameterError(message=translate("err.parser.category_duplicate", fallback="该解析器类目已配置，请使用编辑修改")  )  # 原消息)

            # 关联校验：解析器必须存在、active、且其 parser_type 与本行类目一致
            await self._validate_parser_binding(
                session, tenant_id, parser_type, data.get("parser_config_id")
            )

            # SDD: 跨上下文空间一致性校验
            await self._validate_prompt_template_space(
                session, tenant_id, knowledge_base_id, data.get("prompt_template_id"),
            )

            prompt_text = (data.get("prompt_text") or "").strip()

            obj = await repo.create(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                parser_type=parser_type,
                parser_config_id=data.get("parser_config_id"),
                preprocessing_json=self._list_value(data.get("preprocessing_json")),
                postprocessing_json=self._list_value(data.get("postprocessing_json")),
                prompt_text=prompt_text,
                prompt_template_id=data.get("prompt_template_id"),
                prompt_template_version=data.get("prompt_template_version"),
                summary_prompt_text=data.get("summary_prompt_text") or "",
                summary_template_id=data.get("summary_template_id"),
                summary_template_version=data.get("summary_template_version"),
                tag_prompt_text=data.get("tag_prompt_text") or "",
                tag_template_id=data.get("tag_template_id"),
                tag_template_version=data.get("tag_template_version"),
                status=data.get("status") or "active",
            )

            # [jonex] 主解析提示词联动：非空且类目有映射 → 建 atomic-rag prompt 配置。
            # create_prompt 失败会向上抛 → 事务回滚（行不落库），即"失败阻断"。
            created_prompt_id = None
            if prompt_text:
                created_prompt_id = await self._create_prompt_if_mapped(
                    tenant_id, knowledge_base_id, parser_type, prompt_text
                )
                obj.prompt_config_id = created_prompt_id

            try:
                await session.commit()
            except Exception:
                # commit 失败 → 补偿删除已建 prompt，避免 orphan（best-effort）
                if created_prompt_id:
                    await self._safe_delete_prompt(tenant_id, created_prompt_id)
                raise

            parser_map = await self._load_parser_map(session, tenant_id)
            return self._enrich(obj.to_dict(), parser_map)

    async def update_setting(self, tenant_id: str, setting_id: str, data: dict) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeParserSettingRepository(session)
            obj = await repo.get_required(setting_id, tenant_id)

            old_prompt_config_id = obj.prompt_config_id
            old_parser_type = obj.parser_type

            next_parser_type = data.get("parser_type")
            if next_parser_type is not None:
                next_parser_type = self._normalize(next_parser_type)
                duplicate = await repo.get_by_kb_parser_type(
                    tenant_id, obj.knowledge_base_id, next_parser_type
                )
                if duplicate is not None and duplicate.id != obj.id:
                    raise InvalidParameterError(message=translate("err.parser.category_duplicate", fallback="该解析器类目已配置，请使用编辑修改")  )  # 原消息)
                obj.parser_type = next_parser_type

            updatable = {
                "knowledge_base_id", "parser_config_id", "prompt_text",
                "prompt_template_id", "prompt_template_version", "summary_prompt_text",
                "summary_template_id", "summary_template_version", "tag_prompt_text",
                "tag_template_id", "tag_template_version", "status",
            }
            for key in updatable:
                if key in data:
                    setattr(obj, key, data[key])
            if "preprocessing_json" in data:
                obj.preprocessing_json = self._list_value(data.get("preprocessing_json"))
            if "postprocessing_json" in data:
                obj.postprocessing_json = self._list_value(data.get("postprocessing_json"))

            # 关联校验：以更新后的最终 (parser_type, parser_config_id) 组合为准
            await self._validate_parser_binding(
                session, tenant_id, obj.parser_type, obj.parser_config_id
            )

            # SDD: 跨上下文空间一致性校验（仅 prompt_template_id 变更时校验）
            if "prompt_template_id" in data:
                await self._validate_prompt_template_space(
                    session, tenant_id, obj.knowledge_base_id, data.get("prompt_template_id"),
                )

            # [jonex] 主解析提示词三态迁移（含 parser_type 变更导致 prompt_code 变 → 删旧建新）。
            # prompt 副作用失败向上抛 → 回滚；仅"新建"分支在 commit 失败时补偿删除。
            new_created_prompt_id = await self._sync_prompt_on_update(
                tenant_id, obj, old_parser_type, old_prompt_config_id
            )

            await session.flush()
            try:
                await session.commit()
            except Exception:
                if new_created_prompt_id and obj.prompt_config_id == new_created_prompt_id:
                    await self._safe_delete_prompt(tenant_id, new_created_prompt_id)
                raise
            parser_map = await self._load_parser_map(session, tenant_id)
            return self._enrich(obj.to_dict(), parser_map)

    async def delete_setting(self, tenant_id: str, setting_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            repo = KnowledgeParserSettingRepository(session)
            # 先读出 prompt_config_id，再删 atomic-rag prompt（幂等）；prompt 删除失败 →
            # 抛异常、不删解析器（失败阻断）。delete_prompt 对"不存在"视为成功。
            obj = await repo.get_required(setting_id, tenant_id)
            prompt_config_id = obj.prompt_config_id
            if prompt_config_id:
                await get_rag_client().delete_prompt(tenant_id, prompt_config_id)
            deleted = await repo.delete_soft(setting_id, tenant_id)
            await session.commit()
        return {"deleted": deleted, "id": setting_id}

    # ── 主解析提示词联动辅助 ──────────────────────────────────

    def _prompt_display_name(self, knowledge_base_id: str, parser_type: str) -> str:
        return f"KB {knowledge_base_id}:{parser_type} 主解析提示词"

    async def _create_prompt_if_mapped(
        self, tenant_id: str, knowledge_base_id: str, parser_type: str, content: str
    ) -> Any:
        """按类目映射在 atomic-rag 建 prompt 配置；未映射类目仅记 warning、不建、返回 None。"""
        target = prompt_target(parser_type)
        if target is None:
            logger.warning(
                "parser_type=%s 无 prompt_code 映射，prompt_text 仅存 KB、不下发 atomic-rag",
                parser_type,
            )
            return None
        content = precheck_prompt_template(content)
        preset_name, prompt_code, category = target
        created = await get_rag_client().create_prompt(
            tenant_id,
            prompt_code=prompt_code,
            content=content,
            preset_name=preset_name,
            category=category,
            display_name=self._prompt_display_name(knowledge_base_id, parser_type),
            language="zh",
        )
        return (created or {}).get("id")

    async def _sync_prompt_on_update(
        self, tenant_id: str, obj, old_parser_type: str, old_prompt_config_id: Any
    ) -> Any:
        """update 时的三态迁移。返回本次新建的 prompt_config_id（供 commit 失败补偿），否则 None。"""
        final_text = (obj.prompt_text or "").strip()
        new_target = prompt_target(obj.parser_type)
        old_target = prompt_target(old_parser_type)
        new_code = new_target[1] if new_target else None
        old_code = old_target[1] if old_target else None

        # 清空文本 → 删旧、清 id
        if not final_text:
            if old_prompt_config_id:
                await get_rag_client().delete_prompt(tenant_id, old_prompt_config_id)
            obj.prompt_config_id = None
            return None

        final_text = precheck_prompt_template(final_text)

        # 有文本但新类目无映射 → 删旧、不建新
        if new_target is None:
            if old_prompt_config_id:
                await get_rag_client().delete_prompt(tenant_id, old_prompt_config_id)
            obj.prompt_config_id = None
            logger.warning(
                "parser_type=%s 无 prompt_code 映射，prompt_text 仅存 KB、不下发", obj.parser_type
            )
            return None

        # code 变更（parser_type 改导致 code 不同）→ 删旧建新
        if old_prompt_config_id and old_code and old_code != new_code:
            await get_rag_client().delete_prompt(tenant_id, old_prompt_config_id)
            new_id = await self._create_prompt_if_mapped(
                tenant_id, obj.knowledge_base_id, obj.parser_type, final_text
            )
            obj.prompt_config_id = new_id
            return new_id

        # 有 id 且 code 未变 → 更新内容
        if old_prompt_config_id:
            await get_rag_client().update_prompt(
                tenant_id, old_prompt_config_id, content=final_text
            )
            obj.prompt_config_id = old_prompt_config_id
            return None

        # 无 id 有文本 → 新建
        new_id = await self._create_prompt_if_mapped(
            tenant_id, obj.knowledge_base_id, obj.parser_type, final_text
        )
        obj.prompt_config_id = new_id
        return new_id

    async def _safe_delete_prompt(self, tenant_id: str, prompt_id: str) -> None:
        """补偿删除（best-effort，不抛）。"""
        try:
            await get_rag_client().delete_prompt(tenant_id, prompt_id)
        except Exception:
            logger.warning("compensate delete_prompt failed: id=%s", prompt_id, exc_info=True)

    async def _validate_parser_binding(
        self, session, tenant_id: str, parser_type: str, parser_config_id: Any
    ) -> None:
        """校验选中的 parser_config 合法且类目一致。

        - parser_config_id 为空：跳过（允许先建行、后绑定解析器）；
        - 解析器不存在 / 已删除：报错；
        - 解析器非 active：报错；
        - 解析器 parser_type 与本行类目不一致：报错。
        """
        if not parser_config_id:
            return
        row = (
            await session.execute(
                text(
                    "SELECT parser_type, status FROM business_domain.parser_configs "
                    "WHERE id = :pid AND is_deleted = 0"
                ),
                {"pid": parser_config_id},
            )
        ).first()
        if row is None:
            raise InvalidParameterError(
                message=translate("err.parser.not_found_or_deleted", params={"parser_config_id": str(parser_config_id)}, fallback=f"解析器 {parser_config_id} 不存在或已删除")  ,  # 原消息
                details={"parser_config_id": parser_config_id},
            )
        if row[1] != "active":
            raise InvalidParameterError(
                message=translate("err.parser.not_enabled", params={"parser_config_id": str(parser_config_id)}, fallback=f"解析器 {parser_config_id} 未启用，无法关联")  ,  # 原消息
                details={"parser_config_id": parser_config_id, "status": row[1]},
            )
        parser_category = self._normalize(row[0] or "")
        if parser_category != parser_type:
            raise InvalidParameterError(
                message=translate("err.parser.category_mismatch", params={"parser_category": parser_category, "parser_type": parser_type}, fallback=f"所选解析器属于类目「{parser_category}」，与配置的类目「{parser_type}」不一致")  ,  # 原消息
                details={
                    "parser_config_id": parser_config_id,
                    "parser_type": parser_type,
                    "parser_category": parser_category,
                },
            )

    async def _load_parser_map(self, session, tenant_id: str) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in await self._load_parsers(session, tenant_id, active_only=False)}

    async def _load_parsers(self, session, tenant_id: str, *, active_only: bool) -> list[dict[str, Any]]:
        status_clause = "AND status = 'active'" if active_only else ""
        # parser_configs 已平台共享化（无 tenant_id 列），全租户共读同一目录
        result = await session.execute(text(f"""
            SELECT id, name, parser_type, file_types, config_json, status
              FROM business_domain.parser_configs
             WHERE is_deleted = 0
               {status_clause}
             ORDER BY created_at ASC
        """))
        return [dict(row._mapping) for row in result]

    @staticmethod
    def _enrich(row: dict, parser_map: dict[str, dict[str, Any]]) -> dict:
        """补充选中解析器的展示信息（不覆盖行自身的 parser_type 类目）。"""
        parser = parser_map.get(row.get("parser_config_id") or "")
        row["parser_name"] = parser.get("name") if parser else ""
        row["parser_file_types"] = parser.get("file_types") if parser else []
        row["parser_status"] = parser.get("status") if parser else None
        return row

    @staticmethod
    def _sort_index(parser_type: str) -> int:
        order = {item: idx for idx, item in enumerate(PARSER_TYPE_ORDER)}
        return order.get(parser_type, len(order))

    @staticmethod
    def _normalize(value: str) -> str:
        return str(value).strip().lower().replace(" ", "_").replace("/", "_")

    @staticmethod
    def _list_value(value: Any) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    # ── 跨上下文空间一致性校验（SDD） ──

    async def _validate_prompt_template_space(
        self, session, tenant_id: str, knowledge_base_id: str, prompt_template_id: str | None,
    ) -> None:
        """校验引用的提示词模板空间与知识库空间一致。

        DDD 跨上下文不变式：knowledge_base 只能引用同空间 business_domain 的 domain 模板。
        system 模板（space_id=NULL）允许跨空间引用。
        """
        if not prompt_template_id:
            return

        # 获取 KB 的 space_id
        kb_row = (
            await session.execute(
                text(
                    "SELECT space_id FROM knowledge_base.knowledge_info "
                    "WHERE id = :kid AND tenant_id = :tid AND is_deleted = 0"
                ),
                {"kid": knowledge_base_id, "tid": tenant_id},
            )
        ).first()
        if kb_row is None:
            raise InvalidParameterError(
                message=translate("err.knowledge_base.not_found", params={"kb_id": knowledge_base_id},
                                  fallback=f"知识库不存在: {knowledge_base_id}"),
                details={"knowledge_base_id": knowledge_base_id},
            )
        kb_space_id = kb_row[0]

        # 获取提示词模板的 space_id 和 scope
        pt_row = (
            await session.execute(
                text(
                    "SELECT space_id, scope FROM business_domain.prompt_templates "
                    "WHERE id = :pid AND is_deleted = 0"
                ),
                {"pid": prompt_template_id},
            )
        ).first()
        if pt_row is None:
            raise InvalidParameterError(
                message=translate("err.prompt.not_found", params={"template_id": prompt_template_id},
                                  fallback=f"提示词模板不存在: {prompt_template_id}"),
                details={"prompt_template_id": prompt_template_id},
            )
        pt_space_id, pt_scope = pt_row

        # system 模板允许跨空间引用；domain 模板必须与 KB 同空间
        if pt_scope == "domain" and pt_space_id and pt_space_id != kb_space_id:
            raise InvalidParameterError(
                message=translate("err.prompt.space_mismatch",
                                  params={"template_id": prompt_template_id},
                                  fallback="提示词模板与知识库不属于同一领域空间，禁止引用"),
                details={
                    "prompt_template_id": prompt_template_id,
                    "template_space_id": pt_space_id,
                    "knowledge_base_space_id": kb_space_id,
                },
            )

    @staticmethod
    def _required(data: dict, key: str) -> str:
        value = data.get(key)
        if not value:
            raise InvalidParameterError(message=translate("err.parser.missing_param", params={"key": key}, fallback=f"缺少参数: {key}")  )  # 原消息)
        return str(value)


__all__ = ["ParserSettingService", "RECOMMENDED_PARSER_BY_TYPE"]
