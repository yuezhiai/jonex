"""知识库能力出口分级守卫（B6 访问侧分级保护 §5.1）。

在 `KnowledgeBaseCapability.execute()` 的空间权限判定之后、dispatch 之前执行，
按**用户可读最高等级**过滤/拒绝以下出口：

* 文档级只读 action（详情/状态/质量/血缘/chunk/解析结果/标签/原文/资产原文）；
* 批量质量报告（过滤后返回新 dict，不报错）；
* `resolve_references`（同时裁剪 `doc_ids` 与 `refs`，保持原顺序）；
* KB 浏览与单 KB 检索（KB 内最高生效 rank 超用户 rank ⇒ 404）。

写 action 不在本模块范围内：它们已有空间/KB 写权限校验；本期不做写侧密级拦截。

⚠ 导入顺序：**先 `jonex_core.common.database`，再 `jonex_core.security`**。
`jonex_core.security` 包的 `__init__` 会拉起认证模块；若顺序反过来，在
capability 首次导入本模块时可能撞上包级循环导入。
"""

from __future__ import annotations

import logging
from typing import Any

from jonex_core.common.audit import schedule_emit
from jonex_core.common.audit_enums import ResourceType
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.security import classification_access as policy

logger = logging.getLogger(__name__)

# 单文档只读 action：取 data["document_id"]，超密级 ⇒ 404。
_DOC_ACTIONS = frozenset({
    "get_document",
    "get_document_status",
    "get_document_quality_report",
    "get_document_provenance",
    "get_document_chunks",
    "get_document_parse_result",
    "get_document_source_spans",
    "get_document_tags",
    "get_chunk",
    "get_raw_url",
    "get_raw_content",
    "get_raw_location",
    "get_asset_raw_location",
    "assert_document_readable",
    # 文档版本链：版本列表与归档下载都属于文档读出口
    "list_document_versions",
    "get_document_version_url",
})

# 批量质量报告：过滤掉超密级文档后继续返回（不报错）。
_BATCH_DOC_ACTIONS = frozenset({"get_document_quality_reports"})

# 引用富化：同时裁剪 doc_ids 与 refs。
_RESOLVE_REFS_ACTIONS = frozenset({"resolve_references"})

# KB 级预检：KB 内最高生效 rank 超用户 rank ⇒ 404。
# 除 KB 浏览外，还覆盖**KB 派生产物**（解析结果 / wiki / 本体图），这些 action
# 都按 `knowledge_base_id` 取数，若只判空间角色会绕过文档密级（M2）。
_KB_ACTIONS = frozenset({
    "list_documents", "documents_stats",
    # 网关流式检索的写前校验（B2）：直连 Sidecar 的路由靠它补一次密级预检
    "assert_kb_searchable",
    "get_knowledge_info",
    # 解析结果
    "get_parse_result_summary", "get_parse_result_documents",
    "get_parse_result_entities", "get_parse_result_relationships",
    "get_parse_result_graph_summary", "get_parse_result_graph",
    # Wiki 派生产物
    "get_wiki_page", "list_wiki_contents", "get_wiki_backlinks",
    "get_wiki_claims", "get_wiki_outline",
    # 本体图
    "get_ontology_graph", "list_ontology_instances",
    "list_ontology_relations", "get_ontology_statistics",
    "expand_ontology_neighbors", "search_ontology_entities",
    # Wiki 证据
    "resolve_wiki_evidence",
    # Wiki 编译状态 / 编辑器状态（KB 派生产物的读侧）
    "get_wiki_compilation_state", "get_editor_state",
    # schema（compiled / LLM-Wiki）：由 KB 内文档抽取而来
    "get_compiled_schema", "get_llm_wiki_schema",
    # 标签 / 文件夹（KB 组织元数据，属于该 KB 的读侧视图）
    "list_tags", "list_folders",
})

# 单 KB 检索：只有 DTO 真正消费单个 `knowledge_base_id` 的两个入口。
# `search_llmwiki` / `search_jonex_wiki` / `deep_query` / `query_with_ontology`
# 的 DTO 消费 `knowledge_base_ids`（多 KB）：由 `_make_search_handler` 过滤，
# 全部被过滤时返回统一空结果，**不按单 KB 404**。
_SINGLE_KB_SEARCH_ACTIONS = frozenset({
    "search",
    "search_enhanced",
})

_DOC_DENIED = "文档不存在"
_KB_DENIED = "知识库不存在或无权访问"


async def guard_capability_request(
    tenant_id: str,
    user_id: str | None,
    action: str,
    data: dict[str, Any],
    *,
    is_tenant_admin: bool,
) -> dict[str, Any]:
    """在 dispatch 前执行出口分级守卫，返回（可能被裁剪的）``data``。

    租户管理员一律原样放行；其余 action 若不在受保护分组内也原样返回。
    """
    if is_tenant_admin:
        return data

    if action in _BATCH_DOC_ACTIONS:
        return await _guard_batch_documents(tenant_id, user_id, data)

    if action in _RESOLVE_REFS_ACTIONS:
        return await _guard_resolve_references(tenant_id, user_id, data)

    if action in _DOC_ACTIONS:
        return await _guard_single_document(tenant_id, user_id, action, data)

    if action in _KB_ACTIONS or action in _SINGLE_KB_SEARCH_ACTIONS:
        return await _guard_single_kb(tenant_id, user_id, data)

    return data


async def _guard_single_document(
    tenant_id: str, user_id: str | None, action: str, data: dict[str, Any],
) -> dict[str, Any]:
    document_id = data.get("document_id")
    if not document_id:
        # 必填校验（validate_input / dispatch）会给出更准确的报错，这里不抢答。
        return data
    async with get_db_session() as session:
        allowed = await policy.filter_allowed_documents(
            session, tenant_id, user_id, [document_id],
            is_tenant_admin=False,
        )
    if not allowed:
        required_rank, user_rank = await _denial_ranks(
            tenant_id, user_id, document_id,
        )
        _emit_document_denied(
            tenant_id, user_id, action, document_id, required_rank, user_rank,
        )
        raise ResourceNotFoundError(message=_DOC_DENIED)
    return data


async def _denial_ranks(
    tenant_id: str, user_id: str | None, document_id: str,
) -> tuple[int, int | None]:
    """拒绝路径上补查两端 rank，供审计记录（只在将被拒绝时执行）。

    审计补查失败不能把已经确定的 404 变成 500 —— 兜底返回 `(0, None)`。
    """
    try:
        async with get_db_session() as session:
            ranks = await policy.document_level_ranks(
                session, tenant_id, [document_id],
            )
            user_rank = await policy.get_user_max_rank(
                session, tenant_id, user_id, is_tenant_admin=False,
            )
        return ranks.get(document_id, 0), user_rank
    except Exception:
        logger.warning(
            "分类拒绝审计补查失败（仍返回 404）: doc=%s", document_id,
            exc_info=True,
        )
        return 0, None


def _emit_document_denied(
    tenant_id: str,
    user_id: str | None,
    action: str,
    document_id: str,
    required_rank: int,
    user_rank: int | None,
) -> None:
    """文档级拒绝审计（§5.4）。审计失败不影响拒绝本身。"""
    try:
        uid: int | None = (
            int(user_id) if user_id and user_id != "anonymous" else None
        )
    except (TypeError, ValueError):
        uid = None
    try:
        schedule_emit({
            "tenant_id": tenant_id,
            "user_id": uid,
            "log_type": "OPERATION",
            "action": "classification_access_denied",
            # 审计枚举里没有 Outcome 类型/DENIED，沿用项目既有的 FAILED 口径
            "outcome": "FAILED",
            "service_name": "knowledge_base",
            "resource": ResourceType.DOCUMENT.value,
            "resource_id": str(document_id),
            "request_params": {
                "action": action,
                "required_rank": required_rank,
                "user_rank": user_rank,
            },
        })
    except Exception:  # pragma: no cover - schedule_emit 自身已吞异常
        logger.warning(
            "classification_access_denied 审计调度失败: doc=%s", document_id,
        )


async def _guard_batch_documents(
    tenant_id: str, user_id: str | None, data: dict[str, Any],
) -> dict[str, Any]:
    # 内部标记只能由本守卫在「全被过滤」时设置；先清掉客户端注入，
    # 否则带上 `_classification_all_filtered: true` 就能让批量摘要恒空。
    data = dict(data)
    data.pop("_classification_all_filtered", None)
    document_ids = list(data.get("document_ids") or [])
    if not document_ids:
        return data
    async with get_db_session() as session:
        allowed = await policy.filter_allowed_documents(
            session, tenant_id, user_id, document_ids,
            is_tenant_admin=False,
        )
    if not allowed:
        # 全被过滤：打内部标记，dispatch 直接返回 {"items": []}。
        # 传空 list 给服务会撞它 1..100 的入参闸（400）。
        logger.info(
            "批量质量报告：%d 份文档全部超密级，本次跳过",
            len(document_ids),
        )
        return dict(data, document_ids=[], _classification_all_filtered=True)
    if len(allowed) < len(document_ids):
        logger.info(
            "批量质量报告：跳过 %d 份超密级文档",
            len(document_ids) - len(allowed),
        )
    return dict(data, document_ids=allowed)


async def _guard_resolve_references(
    tenant_id: str, user_id: str | None, data: dict[str, Any],
) -> dict[str, Any]:
    doc_ids = [str(d) for d in (data.get("doc_ids") or [])]
    refs = data.get("refs")
    refs = refs if isinstance(refs, list) else None
    ref_doc_ids = [
        str(r["doc_id"]) for r in (refs or [])
        if isinstance(r, dict) and r.get("doc_id")
    ]
    all_ids = doc_ids + ref_doc_ids
    if not all_ids:
        return data

    async with get_db_session() as session:
        allowed = await policy.filter_allowed_documents(
            session, tenant_id, user_id, all_ids,
            is_tenant_admin=False,
        )
    allowed_set = set(allowed)
    skipped = len(set(all_ids)) - len(set(allowed))
    if skipped:
        logger.info("引用富化：跳过 %d 个超密级文档引用", skipped)

    result = dict(data)
    if doc_ids:
        # 保持原顺序与重复项：按原列表逐个筛选。
        result["doc_ids"] = [d for d in doc_ids if d in allowed_set]
    if refs is not None:
        # 无 doc_id 的 ref 无法判定、下游也会忽略，原样保留。
        result["refs"] = [
            r for r in refs
            if not (isinstance(r, dict) and r.get("doc_id"))
            or str(r["doc_id"]) in allowed_set
        ]
    return result


async def _guard_single_kb(
    tenant_id: str, user_id: str | None, data: dict[str, Any],
) -> dict[str, Any]:
    # 绝大多数 action 用 `knowledge_base_id`；`get_knowledge_info`（B2）沿用
    # 服务自身的 `kb_id` 字段，两者都认。
    kb_id = data.get("knowledge_base_id") or data.get("kb_id")
    if not kb_id:
        return data
    async with get_db_session() as session:
        max_ranks = await policy.kb_max_level_rank(session, tenant_id, [kb_id])
        user_rank = await policy.get_user_max_rank(
            session, tenant_id, user_id, is_tenant_admin=False,
        )
    if user_rank is not None and max_ranks.get(kb_id, 0) > user_rank:
        raise ResourceNotFoundError(message=_KB_DENIED)
    return data
