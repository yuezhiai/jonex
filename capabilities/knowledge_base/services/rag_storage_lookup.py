# -*- coding: utf-8 -*-
"""[jonex] LightRAG storage 文档反查 —— 分页与匹配的单一实现。

`rag_doc_ids` 为空（reparse 覆盖、insert 未回填、对账未及时补充）时，删除链路
（document_service）与对账链路（reconciliation_service）都要从 LightRAG 反查该
文档的 doc_ids。两处曾各写一份，且都违反 LightRAG 真实契约，任一都足以让兜底
恒失败、删除静默跳过 LightRAG 清理：

  - `page_size=500`：`/documents/paginated` 约束 page_size ∈ [10, 200]，越界
    直接 422，平台侧表现为 `RAG[get_storage_documents] 调用失败`；
  - 读 `result["items"]`：真实响应顶层键是 `documents`（配 `pagination` /
    `status_counts`），`items` 不存在，恒得空列表。

2026-09-16 kb_demo_internet 删除后残留 8 chunk / 240 图节点即由此产生。此处收敛
为单一实现，契约集中维护，避免两份拷贝再次漂移。
"""
import logging
import re

logger = logging.getLogger(__name__)

# LightRAG POST /documents/paginated 的约束与响应键
PAGE_SIZE = 200  # 上限：约束区间 [10, 200]，取上限以减少往返
DOCS_KEY = "documents"  # 响应顶层键（不是 items）
MAX_PAGES = 50  # 防御上游 has_next 异常时的死循环

# file_path 锚点形如 `kb=..|doc=<uuid>|tenant=..|file=..`
_DOC_IN_FILE_PATH = re.compile(r"doc=([a-f0-9-]+)\|")


def _match_by_document_id(items: list, document_id: str) -> list:
    """主判据：file_path 里的 `doc=<uuid>`。

    比 file_name 可靠——LightRAG 会加 UUID 前缀，且中文文件名可能被改写成下划线。
    """
    matched = []
    for item in items:
        m = _DOC_IN_FILE_PATH.search(item.get("file_path") or "")
        if m and m.group(1) == document_id:
            matched.append(item)
    return matched


def _match_by_file_name(items: list, file_name: str) -> list:
    """兜底判据：老数据 file_path 无 `doc=` 锚点时退回文件名匹配。"""
    return [
        item for item in items
        if item.get("file_name") == file_name
        or (item.get("file_name") or "").endswith("_" + file_name)
    ]


async def find_rag_doc_ids_by_document(
    get_client,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
) -> list | None:
    """按 `doc=<id>` 锚点反查 LightRAG doc_ids（B11）。

    LightRAG 的 `/documents/paginated` 有 jonex 扩展 `doc_id`：按 `file_source`
    里的 `doc=<id>` 锚点过滤。部分 chunk 文档的 `file_name` 为 null，关键词筛选
    永远筛不到它们，必须走这个过滤。

    **三态**（与 `find_rag_doc_ids` 的「失败即空」不同，这里不能合并）：
    - `list`（可空）：查询成功；`[]` = 确认没有该文档的旧内容；
    - `None`：查询失败 —— 调用方**必须拒绝**清旧/重解析，不能把「查不了」
      当成「没有」。
    """
    if not knowledge_base_id or not document_id:
        return []
    try:
        result = await get_client().get_storage_documents(
            knowledge_base_id=knowledge_base_id,
            tenant_id=tenant_id,
            page=1,
            page_size=PAGE_SIZE,
            document_id=document_id,
        ) or {}
        items = result.get(DOCS_KEY) or []
        # 服务端已按 doc_id 过滤；这里再匹配一次，防旧版服务端忽略该参数时
        # 把整页文档当成命中。
        matched = _match_by_document_id(items, document_id)
        return [item["id"] for item in matched if item.get("id")]
    except Exception:
        logger.error(
            "Storage lookup by document failed: doc_id=%s", document_id,
            exc_info=True,
        )
        return None


async def find_rag_doc_ids(
    get_client,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    file_name: str,
    document_id: str,
) -> list:
    """分页反查 LightRAG 中属于 document_id 的 doc_ids；未命中或调用失败返回 []。

    `get_client` 由调用方以工厂形式传入（而非在此 import），便于测试注入，也让
    取客户端的失败同样落在本函数的异常兜底内。异常在此吞掉并记 ERROR ——
    调用方据此决定是跳过清理（删除链路）还是视为未能确认（对账链路）。
    """
    if not file_name or not knowledge_base_id:
        return []

    try:
        client = get_client()
        seen: list = []
        matched: list = []
        page = 1
        while page <= MAX_PAGES:
            result = await client.get_storage_documents(
                knowledge_base_id=knowledge_base_id,
                tenant_id=tenant_id,
                keyword=file_name,
                page=page,
                page_size=PAGE_SIZE,
            ) or {}
            items = result.get(DOCS_KEY) or []
            if not items:
                break
            seen.extend(items)
            matched = _match_by_document_id(items, document_id)
            if matched:
                break
            # 主判据未命中才继续翻页；分页信息在 pagination，不在 total
            if not (result.get("pagination") or {}).get("has_next"):
                break
            page += 1

        # 全部页主判据都没命中，才落到文件名兜底——逐页兜底会把
        # 首页里他文档的同名文件误判成本文档，导致删错数据。
        if not matched and seen:
            matched = _match_by_file_name(seen, file_name)

        if not matched:
            logger.info(
                "Storage lookup miss: doc_id=%s, file_name=%s, scanned=%d",
                document_id, file_name, len(seen),
            )
            return []

        doc_ids = [item["id"] for item in matched if item.get("id")]
        logger.info(
            "Storage lookup hit: doc_id=%s, found %d LightRAG doc_ids",
            document_id, len(doc_ids),
        )
        return doc_ids

    except Exception:
        logger.error(
            "Storage lookup failed for doc %s (file_name=%s)：该文档的 LightRAG "
            "数据可能无法清理", document_id, file_name, exc_info=True,
        )
        return []
