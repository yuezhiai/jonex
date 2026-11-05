# -*- coding:utf-8 -*-
"""演化台 W4 Task 3：`EVOLUTION_UPSTREAM=router` 的 HTTP asker。

把一个「问题」发到 router 的 `/api/v1/knowledge-base/search/data-only`，再把
data-only 响应经 `router_adapter.outcome_from_data_only` 映射成与进程内
`Nl2SqlService.ask_data_source` 同形的 outcome（eval / smoke / gate / bench 零改动）。

控制器裁定（优先于设计稿的含糊处）：

- **阻塞调用必须离事件循环**：HTTP 走模块级同步函数 `_post_data_only`
  （测试可打桩），在 `ask` 里用 `asyncio.to_thread` 调用；绝不在协程里直接
  `urllib`。
- **每题现签 token**：`tokens.issue_evolution_token(tenant_id, _router_user_id())`
  （`_router_user_id()` 读 `EVOLUTION_ROUTER_USER_ID`，默认 `"1"`）在每次 `ask`
  里签一次（30 分钟 TTL 足够单题；长评测靠每题续签，避免中途 401）。
- **失败分档**：HTTPError 的 body 是 JSON 对象 → 交给 adapter（`{success:false,
  error}` 信封自然映射成 `llm_failed`）；body 不是 JSON → `UpstreamServiceError`。
  连接 / 超时 / 2xx 坏 JSON / 2xx 非 data-only 信封（`data` 不是 dict）→ 抛
  `UpstreamServiceError`（eval/smoke 记为 error，不静默伪装成「答不了」）。
- **ds 为空**：alias 等 KB 级语料定位不到源，直接返回显式不可答（不发请求、
  不签 token），形状复用 adapter 的构造器以免 11 键漂移。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import UpstreamServiceError

from . import tokens
from . import upstream as upstream_mod
from .router_adapter import _outcome as _adapter_outcome
from .router_adapter import outcome_from_data_only

logger = logging.getLogger(__name__)

#: data-only 路由（router 拦下并只跑数据链）。
_DATA_ONLY_PATH = "/api/v1/knowledge-base/search/data-only"


def _no_ds_outcome() -> dict:
    """`ds` 为空 → 显式不可答（不发请求、不签 token）。

    复用 `router_adapter._outcome`（而不是另写一份 dict），保证与
    `outcome_from_data_only` 的 11 键形状**不会漂移**。
    """
    return _adapter_outcome(
        ok=False, not_answerable=True,
        reason_code="no_candidate", reason="router 模式需要数据源")


def _router_user_id() -> str:
    """演化 JWT 的 `sub`：需要有目标 KB 空间权限的**数值**用户。

    `tokens.issue_evolution_token` 会把非数值 `sub` 退回 `0`，而 Jonex 的空间
    守卫会拒掉 `sub=0`（真机：`kind=error, "No permission to operate on space"`）。
    配置为空/空白时回落 `"1"`；username 仍是 `evolution`，审计可辨识。
    """
    return str(get_config().EVOLUTION_ROUTER_USER_ID or "1").strip() or "1"


def _post_data_only(url: str, body_bytes: bytes, headers: dict,
                    timeout: float) -> bytes:
    """阻塞 HTTP POST（模块级、测试可打桩）：返回响应体**原始字节**。

    由 `ask` 通过 `asyncio.to_thread` 调用 —— 这里读 socket 会阻塞线程，
    绝不能直接在事件循环里执行。
    """
    req = urllib.request.Request(
        url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_json(raw) -> object:
    """解析响应体；失败抛 `ValueError`（由调用方收成 `UpstreamServiceError`）。"""
    return json.loads(raw)


def _body_excerpt(raw, limit: int = 200) -> str:
    """响应体摘要（压缩空白、截断）：塞进错误消息便于定位错配的 `EVOLUTION_ROUTER_BASE`。"""
    if not raw:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    else:
        text = str(raw)
    return " ".join(text.split())[:limit]


def router_asker_for(tenant_id: str, kb: str, ds: str | None, *,
                     evolution_run_id: str = ""):
    """同步工厂（与 `upstream.asker_for` 同形）→ `async def ask(question) -> dict`。

    `evolution_run_id` 在**工厂时**归一一次（去空白、截 64 字符），非空才进
    body —— router 据此把该路流量标记为合成（演化评测），普通流量形状不变。
    """
    run_id = str(evolution_run_id or "").strip()[:64]

    async def ask(question: str) -> dict:
        if not ds:
            # KB 级语料（alias 等）没有数据源：router 模式无法定位源，不猜。
            return _no_ds_outcome()

        base = str(get_config().EVOLUTION_ROUTER_BASE or "").rstrip("/")
        url = f"{base}{_DATA_ONLY_PATH}"
        token = tokens.issue_evolution_token(tenant_id, _router_user_id())
        body = {
            "query": question,
            "knowledge_base_id": kb,
            "data_source_ids": [ds],
        }
        if run_id:
            body["evolution_run_id"] = run_id
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": tenant_id,
            "Content-Type": "application/json",
        }
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        try:
            raw = await asyncio.to_thread(
                _post_data_only, url, body_bytes, headers,
                upstream_mod._router_timeout())
        except urllib.error.HTTPError as exc:
            # 只有 router 自己的信封（含 `success` / `data` 的 JSON 对象）才交给
            # adapter；其它 JSON（网关 `{code,message}`、代理派生的 JSON）是
            # 链路/配置错，必须上抛。否则 adapter 会把无形状 body 映射成
            # `no_candidate`，被 scorer 当成合规拒答而不是 error。
            raw_body = b""
            try:
                raw_body = exc.read()
                payload = _parse_json(raw_body)
            except Exception:  # noqa: BLE001 —— 读不出/不是 JSON 就按链路错处理
                payload = None
            if (isinstance(payload, dict)
                    and ("success" in payload or "data" in payload)
                    and isinstance(payload.get("data", {}), dict)):
                return outcome_from_data_only(payload)
            excerpt = _body_excerpt(raw_body)
            detail = f"：{excerpt}" if excerpt else ""
            raise UpstreamServiceError(
                message=f"router 返回 HTTP {exc.code}：data-only 调用失败"
                        f"（响应不是 data-only 信封）{detail}") from exc
        except urllib.error.URLError as exc:
            raise UpstreamServiceError(
                message=f"router 不可达：data-only 调用失败（{exc.reason}）") from exc
        except (OSError, ValueError) as exc:
            raise UpstreamServiceError(
                message=f"router 不可达或响应不可解析：data-only 调用失败（{exc}）") from exc

        try:
            payload = _parse_json(raw)
        except (TypeError, ValueError) as exc:
            raise UpstreamServiceError(
                message=f"router 响应不是 JSON：data-only 调用失败（{exc}）") from exc
        # 2xx 也必须是 data-only 信封（dict 且 `data` 是 dict）：否则是错配的
        # `EVOLUTION_ROUTER_BASE` / 代理派生 JSON，必须上抛而不是映射成
        # `no_candidate`（那会被 scorer 当成合规拒答）。
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            excerpt = _body_excerpt(raw)
            detail = f"：{excerpt}" if excerpt else ""
            raise UpstreamServiceError(
                message="router 返回 HTTP 2xx：data-only 调用失败"
                        f"（响应不是 data-only 信封）{detail}")

        outcome = outcome_from_data_only(payload)
        if (payload["data"].get("engine_order") == "di_first"
                and not outcome.get("ok")):
            logger.warning(
                "router data-only 为 di_first 且未返回行：该题不可判分"
                "（DI 答案不含 table）")
        return outcome

    return ask


__all__ = ["router_asker_for"]
