#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""KB 级模型配置服务（P1：存储 + 只读展示；P2/P3 才让它生效）。

职责边界
--------
- **本服务只负责**：读写覆盖值、按 KB 类型决定哪几行可见/可写、把"当前生效的是什么"
  如实告诉前端。它**不**决定任务用哪个模型 —— 那是派活时的 ``resolve_for_dispatch``。
- **不下发 env 默认值**：平台容器与引擎容器的 env 不同源（``.env`` vs ``.env.jonex_wiki``），
  平台读到的值未必是引擎实际用的值。所以无覆盖时 ``effective`` 返回 ``None``，
  由前端显示「部署默认（引擎侧）」，而不是编造一个可能错误的具体值。

设计见 ``docs/knowledge-base/kb-model-settings-design.md`` §6/§7.2。
"""

import logging

from jonex_core.common import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository.knowledge_info_repository import KnowledgeInfoRepository
from ..repository.model_setting_repository import KnowledgeModelSettingRepository
from . import kb_model_config

logger = logging.getLogger(__name__)

#: 模型名长度上限（与列宽一致）
_MODEL_NAME_MAX_LEN = 255

#: 模型名里不允许出现的字符：空白/逗号/引号/斜杠。"deepseek/deepseek-flash"
#: 这类带斜杠的名字是合法的，所以斜杠必须保留 —— 只挡会把下游 payload 弄脏的字符。
_FORBIDDEN_CHARS = set(' \t\r\n,;"\'')


def _validate_model_name(raw: object) -> str:
    """校验模型名；空字符串由调用方按"清空"处理，不走这里。"""
    value = str(raw or "").strip()
    if not value:
        raise InvalidParameterError(
            message=translate("err.kb.model_name_required", fallback="模型名不能为空")
        )
    if len(value) > _MODEL_NAME_MAX_LEN:
        raise InvalidParameterError(
            message=translate(
                "err.kb.model_name_invalid",
                params={"max": _MODEL_NAME_MAX_LEN},
                fallback=f"模型名过长（最多 {_MODEL_NAME_MAX_LEN} 个字符）",
            )
        )
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        raise InvalidParameterError(
            message=translate(
                "err.kb.model_name_invalid",
                params={"max": _MODEL_NAME_MAX_LEN},
                fallback="模型名不能包含空白、逗号、分号或引号",
            )
        )
    return value


async def resolve_dispatch_models(
    tenant_id: str,
    knowledge_base_id: str,
    *,
    kb_type: str = "",
    ingest_backend: str = "",
) -> dict[str, str]:
    """[jonex] P2b/P3a：**派活时该下发什么** —— 全平台唯一的解析入口。

    返回 ``{scope: model_name}``，只含"真该下发"的项：

    - 该 scope 对这个库（类型 / 链路）没有意义的不出现（``configurable_scopes``）；
    - 没有行 / 空值的不出现。

    调用方约定：**取不到就什么都不带**（没有默认值兜底）。引擎那边"没收到"与
    "平台没配置"必须是同一件事 —— 都走引擎自己的默认链（注册表 → env），
    平台不替引擎决定默认值。

    ⚠️ **fail-open**：读配置表失败（表未建 / 连接抖动）返回 ``{}`` 并打 warning，
    不抛。模型覆盖是增强项，让它把一次 DB 抖动升级成"整份文档解析失败 / 整个
    对账轮次中断"，是把增强做成了单点。

    为什么是**模块级函数**而不是 ``ModelSettingService`` 的方法：两个写入者
    （``ontology_service.retry_extract`` 与 ``reconciliation_service`` 的对账重抽）
    以及 ``DocumentService`` 都要用，套一层实例只会让调用方各自 new 一个服务对象。
    """
    tenant_id = require_tenant(tenant_id)
    try:
        async with get_db_session() as session:
            rows = await KnowledgeModelSettingRepository(session).list_by_kb(
                tenant_id, knowledge_base_id
            )
        return kb_model_config.resolve_for_dispatch(
            {row.scope: row.model_name for row in rows},
            kb_type=kb_type,
            ingest_backend=ingest_backend,
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring：增强项不拦主流程
        logger.warning(
            "[jonex] 读取 KB 级模型覆盖失败（按无覆盖处理）kb=%s: %s",
            knowledge_base_id, exc,
        )
        return {}


class ModelSettingService:
    """知识库模型配置读写。"""

    async def list_settings(self, tenant_id: str, knowledge_base_id: str) -> dict:
        tenant_id = require_tenant(tenant_id)
        async with get_db_session() as session:
            kb = await self._require_kb(session, tenant_id, knowledge_base_id)
            repo = KnowledgeModelSettingRepository(session)
            rows = await repo.list_by_kb(tenant_id, knowledge_base_id)
            overrides = {row.scope: row for row in rows}
            return {
                "knowledge_base_id": knowledge_base_id,
                "kb_type": kb.kb_type or "",
                "items": self._build_items(
                    kb_type=kb.kb_type or "",
                    ingest_backend=kb.ingest_backend or "",
                    overrides=overrides,
                ),
            }

    async def save_settings(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        items: list[dict],
        *,
        user_id: str | None = None,
    ) -> dict:
        """批量写入/清空覆盖值，返回与 ``list_settings`` 同结构。

        语义：``model_name`` 为空 ⇒ 硬删该行（恢复继承）；一行一条 scope。
        """
        tenant_id = require_tenant(tenant_id)
        if not isinstance(items, list) or not items:
            raise InvalidParameterError(message="items 不能为空")

        async with get_db_session() as session:
            kb = await self._require_kb(session, tenant_id, knowledge_base_id)
            kb_type = kb.kb_type or ""
            ingest_backend = kb.ingest_backend or ""
            writable = kb_model_config.configurable_scopes(kb_type, ingest_backend)

            repo = KnowledgeModelSettingRepository(session)
            seen: set[str] = set()
            for item in items:
                scope = str((item or {}).get("scope") or "").strip()
                if scope in seen:
                    raise InvalidParameterError(message=f"同一请求里重复出现 scope: {scope}")
                seen.add(scope)
                if scope not in writable:
                    raise InvalidParameterError(
                        message=translate(
                            "err.kb.model_scope_not_configurable",
                            params={"scope": scope},
                            fallback=f"「{scope}」不支持按知识库配置",
                        )
                    )
                raw_name = (item or {}).get("model_name")
                if raw_name is None or not str(raw_name).strip():
                    await repo.delete_by_scope(tenant_id, knowledge_base_id, scope)
                    continue
                validated = _validate_model_name(raw_name)
                await repo.upsert(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    scope=scope,
                    model_name=validated,
                    actor_user_id=user_id,
                )

            await session.commit()

            rows = await repo.list_by_kb(tenant_id, knowledge_base_id)
            overrides = {row.scope: row for row in rows}
            result = {
                "knowledge_base_id": knowledge_base_id,
                "kb_type": kb_type,
                "items": self._build_items(
                    kb_type=kb_type, ingest_backend=ingest_backend, overrides=overrides
                ),
            }

        # [jonex] P3b：推送失败**不阻断保存** —— 平台表是配置的权威，引擎侧只是投影；
        # 下次保存会再推一次（引擎每次编译都读实时值），不存在要人工修的死角。
        # [jonex] P3b（方案 B）：保存时不再向引擎推送任何副本。编译模型由
        # `resolve_dispatch_models()` 在**发起编译/解析任务时**取出来塞进任务报文，
        # 引擎只把值写进那次任务的快照 —— 唯一事实源就是这张平台表。
        return result

    # ── 内部 ──────────────────────────────────────────────────

    async def _require_kb(self, session, tenant_id: str, knowledge_base_id: str):
        """KB 必须存在且属于本租户，否则 404。

        权限闸门在 capability 层：``_resolve_space_context`` 在 KB 不存在时返回
        ``space_id=None``，那道闸门会被整段跳过（capability.py:1704 的既有行为），
        所以这里的 404 是**唯一**的存在性校验。
        """
        repo = KnowledgeInfoRepository(session)
        kb = await repo.get_by_id(knowledge_base_id, tenant_id)
        if kb is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.kb.not_found",
                    params={"kb_id": knowledge_base_id},
                    fallback=f"知识库不存在: {knowledge_base_id}",
                )
            )
        return kb

    def _build_items(
        self,
        *,
        kb_type: str,
        ingest_backend: str,
        overrides: dict,
    ) -> list[dict]:
        items: list[dict] = []

        for scope_def in kb_model_config.visible_scopes(kb_type):
            row = overrides.get(scope_def.scope)
            kb_value = row.model_name if row is not None else None
            configurable = kb_model_config.is_configurable(
                scope_def, kb_type, ingest_backend
            )
            # 部署侧默认值（**只做展示**，不下发）：读的是平台自己这个进程的 env。
            # 线上 kb-service 与引擎容器同源 `deploy/.env`，所以它就是引擎回落时用的那个
            # 变量值；但严格说引擎还有「模型注册表」一层（注册表命中时不用 env），
            # 所以前端把它标成「部署默认」并带变量名，避免被当成「实际调用的模型」。
            engine_default = kb_model_config.env_default(scope_def.scope)
            items.append(
                {
                    "scope": scope_def.scope,
                    "label_key": scope_def.label_key,
                    "label": scope_def.label,
                    "stage": scope_def.stage,
                    # `configurable=false` = 只读展示（向量化行等）：
                    # 它表示"这个 scope 对这本库没有意义"，与"能不能改"是同一件事。
                    # 不再有第二层灰度开关（2026-10 移除）。
                    "configurable": configurable,
                    "kb_value": kb_value,
                    "effective": kb_value,
                    "source": "kb" if kb_value else "engine_default",
                    "source_text": "知识库自定义" if kb_value else "部署默认（引擎侧）",
                    "env_var": scope_def.env_var,
                    "engine_default": engine_default,
                    "disabled_reason": kb_model_config.not_configurable_reason(
                        scope_def, kb_type, ingest_backend
                    ),
                    "updated_at": (
                        row.updated_at.isoformat() if row and row.updated_at else None
                    ),
                    "updated_by": row.updated_by if row else None,
                }
            )

        if kb_model_config.show_embedding_row(kb_type):
            items.append(
                {
                    "scope": kb_model_config.EMBEDDING_SCOPE,
                    "label_key": kb_model_config.EMBEDDING_LABEL_KEY,
                    "label": kb_model_config.EMBEDDING_LABEL,
                    "stage": "compile",
                    "configurable": False,
                    "kb_value": None,
                    # 只读展示实际部署值：kb-service 与 lightrag 共用同一个 EMBEDDING_MODEL
                    "effective": kb_model_config.env_default(kb_model_config.EMBEDDING_SCOPE),
                    "source": "engine_default",
                    "source_text": "部署默认（kb-service 与 LightRAG 共用）",
                    "env_var": kb_model_config.EMBEDDING_ENV_VAR,
                    "engine_default": kb_model_config.env_default(kb_model_config.EMBEDDING_SCOPE),
                    "disabled_reason": "LightRAG 不支持按知识库单独配置；改此值需重建全部向量索引",
                    "updated_at": None,
                    "updated_by": None,
                }
            )

        if kb_model_config.show_rerank_row(kb_type):
            rerank_default = kb_model_config.env_default(kb_model_config.RERANK_SCOPE)
            items.append(
                {
                    "scope": kb_model_config.RERANK_SCOPE,
                    "label_key": kb_model_config.RERANK_LABEL_KEY,
                    "label": kb_model_config.RERANK_LABEL,
                    "stage": kb_model_config.RERANK_STAGE,
                    "configurable": False,
                    "kb_value": None,
                    # 只读展示实际部署值：检索时平台把这个模型名随请求下发
                    "effective": rerank_default,
                    "source": "engine_default",
                    "source_text": "部署默认（检索期统一配置）",
                    "env_var": kb_model_config.RERANK_ENV_VAR,
                    "engine_default": rerank_default,
                    "disabled_reason": "重排模型由部署侧统一配置（RERANK_MODEL / 网关 LLMGW_RERANK_MODEL），不支持按知识库覆盖",
                    "updated_at": None,
                    "updated_by": None,
                }
            )

        return items


__all__ = ["ModelSettingService"]
