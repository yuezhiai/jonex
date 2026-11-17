# [jonex] 新增文件 — Jonex Wiki 列式浏览业务门面
"""MCP 列式浏览（链接多跳）的业务侧门面。

职责只有三件：

1. **KB 类型校验**：只有 ``jonex_wiki`` 库允许走这套浏览（lightrag / openkb
   显式拒绝为业务 1001，由 MCP 映射成 ``-32602``，不得 500）；
2. **透传全库图**：调 atomic ``list_page_graph``（图源上限已在适配器截断一次）；
3. **降级**：引擎不可用 / 超时 → 返回 ``degraded=true`` 的空增量，**不抛 5xx**。

BFS、``depth`` / ``limit`` / ``hop`` / 链入截断都在 MCP 工具层，不在本服务。
"""
import logging

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

from .jonex_wiki_service import JonexWikiService
from .kb_type_service import get_kb_type

logger = logging.getLogger(__name__)

_JONEX_WIKI_KB_TYPE = "jonex_wiki"


class WikiBrowseService:
    """Jonex Wiki 列式浏览：类型校验 + 全库图透传 + 降级。"""

    def __init__(self):
        # 引擎客户端**延迟构造**（与 WikiOutlineService / parse_result_service 同口径）：
        # 能力模式（remote/mock/local）在运行期解析，构造期实例化会在 LOCAL 兜底
        # 模式下直接抛 ValueError，连累 knowledge_base 能力容器起不来。
        self._wiki = None

    def _wiki_service(self) -> JonexWikiService:
        if self._wiki is None:
            self._wiki = JonexWikiService()
        return self._wiki

    async def get_page_graph(self, tenant_id: str, kb_id: str) -> dict:
        """返回单库带语义全库图（capability action ``get_wiki_page_graph``）。

        正常体在 atomic 原始图上补 ``knowledge_base_id`` / ``degraded=false``；
        ``page_total`` / ``link_total`` 是适配器截断前的真实计数。
        """
        kb_type = await get_kb_type(tenant_id, kb_id)
        if kb_type != _JONEX_WIKI_KB_TYPE:
            raise InvalidParameterError(
                message=translate(
                    "err.kb.not_jonex_wiki",
                    fallback="仅 Jonex Wiki 知识库支持该浏览操作",
                ),
            )

        try:
            raw = await self._wiki_service().list_page_graph(
                kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
            )
        except Exception as exc:  # noqa: BLE001 — 引擎不可用必须降级，不能 5xx
            # 只记 kb_id + 异常类型；异常文本留在服务端日志，不回显给调用方。
            logger.warning(
                "[wiki_browse] 全库图读取失败，返回降级体: kb=%s err=%s",
                kb_id, type(exc).__name__, exc_info=True,
            )
            return {
                "knowledge_base_id": kb_id,
                "pages": [],
                "links": [],
                "page_total": 0,
                "link_total": 0,
                "truncated": False,
                "truncated_reason": [],
                "degraded": True,
                "degraded_reason": "engine_unavailable",
            }

        raw = raw or {}
        return {
            "knowledge_base_id": kb_id,
            "pages": list(raw.get("pages") or []),
            "links": list(raw.get("links") or []),
            "page_total": int(raw.get("page_total") or 0),
            "link_total": int(raw.get("link_total") or 0),
            "truncated": bool(raw.get("truncated", False)),
            "truncated_reason": list(raw.get("truncated_reason") or []),
            "degraded": False,
        }
