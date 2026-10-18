# jonex_core/capability/atomic/ontology_compile/client.py
# -*- coding: utf-8 -*-
"""[jonex] 本体编译「调用侧」客户端（KB 用，设计 §4.3）—— 照同类 capability client 同构。

调用链：`knowledge_base → Sidecar /invoke → atomic.ontology_compile.v1`。
payload 形状与 `jonex_wiki` 等能力钉死同一套：`{action, knowledge_base_id, data}`；
`tenant_id` 走 body 顶层 + `X-Tenant-ID` 头，**不放进 payload**。

只支持 MOCK / REMOTE —— 引擎与适配器同进程（B1），REMOTE 指向的就是那个能力容器。
`LOCAL` 显式报错而不是静默回落。

**与样板 client 的两处有意差异（都是「没有对应物」而非放松判断）**：

1. **没有 `_ENGINE_ERROR_MAP`**：样板那张表是拿引擎 `AppError.code`（字符串）翻平台异常的
   —— 本能力**目前不产生业务错误码**（`capability.py` 的错误面只有
   `CapabilityResponse.error(request_id, 400/404/500, msg)`），故不建那张表：凭空造一张
   只会是**永远命中不了**的映射。故本文件只保留**状态码兜底**（`_STATUS_FALLBACK_MAP`）。

   ⚠️ 但这**不等于**「Sidecar 没有 `details`」—— 这个结论是错的：**Sidecar 的信封是带
   `details` 的**（`sidecar/main.py:90` 的 `details: Optional[dict] = None` 声明、
   `:843` 的 `details=result.get("details")` 在 `/invoke` 成功路径**原样转发**）。将来本能力
   若要输出结构化错误信息，把它放进 `CapabilityResponse.error(..., details={...})` 即可
   透传上来 —— 本 client 已按样板把信封里的 `details`
   带进抛出的异常（见 `_invoke`）。
2. **`success` 缺键视为成功**（`result.get("success", True)`）：样板写的是 `not result.get("success")`。
   Sidecar **自己**读能力结果就是这个口径（`sidecar/proxy.py:146`、`sidecar/main.py:834` 的
   `result.get("success", True)`）—— 缺键是「没这个字段」，不是「受理失败」。判据强度不变：
   显式 `success=false` 仍走异常分支。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from jonex_core.capability.locator import CapabilityMode, get_locator
from jonex_core.common.exceptions import (
    AuthError,
    CapabilityInvokeError,
    InvalidParameterError,
    JonexException,
    OperationNotSupportedError,
    RateLimitExceededError,
    ResourceNotFoundError,
    UpstreamServiceError,
)

CAPABILITY_ID = "atomic.ontology_compile.v1"

# 非 2xx 时带进异常的响应体摘要长度 —— 与 Sidecar 自己 `proxy.py` 里
# `e.response.text[:200]` 同口径。
_BODY_SUMMARY_LIMIT = 200

# 状态码兜底：能力侧的错误响应只有 `code`（无引擎业务码体系，见模块 docstring ①），
# 故唯一可用的判据是「HTTP 状态」与「响应里的 code」——两者在本平台同源
# （`CapabilityResponse.error(request_id, 400/404, …)` 的 400/404 就是 HTTP 状态）。
# 分档理由与样板一致：4xx = 调用方传错了（别重试），5xx = 责任在对面（可重试）。
# `429` 单独成档：invoke 链路上有**租户级限流**（`sidecar/hooks.py` 的
# `RATE_LIMIT_TENANT_PER_MINUTE`，默认 300/min），超限回 `429 + Retry-After`。
# 它塌成 `CapabilityInvokeError`(500) 会让调用方分不清「该退避重试」还是「对面炸了」——
# 两者的处置相反，是这条链上最需要区分的一档。
_STATUS_FALLBACK_MAP: dict[int, type[JonexException]] = {
    400: InvalidParameterError,
    401: AuthError,
    403: AuthError,
    404: ResourceNotFoundError,
    429: RateLimitExceededError,
    502: UpstreamServiceError,
    503: UpstreamServiceError,
    504: UpstreamServiceError,
}


def _response_body_summary(response: Any, limit: int = _BODY_SUMMARY_LIMIT) -> str:
    """取响应体前 `limit` 个字符（照 Sidecar 自己 `proxy.py` 的 `text[:200]`）。

    非 2xx 时把「对面到底说了什么」带进异常：跨容器 `JWT_SECRET` 不一致（部署期高发）
    会让 Sidecar 把上游 401 翻成 `CapabilityInvokeError(500)`，只报「HTTP 500」会把
    排查方向指成「能力坏了」而不是「鉴权配错了」。

    读摘要本身失败（编码怪 / 无 body）返回空串，**不让摘要覆盖原始错误**。
    """
    try:
        return (response.text or "")[:limit]
    except Exception:  # noqa: BLE001 —— 摘要失败不该盖过原始错误
        return ""


class OntologyCompileClient(ABC):
    """knowledge_base 调 ontology_compile 的抽象接口。"""

    @abstractmethod
    async def compile(self, *, document_id: str, knowledge_base_id: str, tenant_id: str,
                      mode: str = "ingest_extract", schema: dict[str, Any] | None = None,
                      chunks: list[dict[str, Any]] | None = None,
                      options: dict[str, Any] | None = None,
                      content_generation: int | None = None) -> dict[str, Any]:
        """投递一次编译，**立刻**返回 `{"compile_id": ..., "status": "queued"}`。

        执行在容器内 worker 后台跑（能力 action 只入队），所以本调用**不是**长调用：
        要等结果靠 `get_compile_status` 轮询，别把等待压在这一次 POST 上。

        `mode`：`extract_only`（rag 链过渡期）。

        `chunks`（过渡兼容通道，P4 后删）：**不传**（默认 `None`）时容器经 Sidecar 调
        chunks 由调用方随任务传入（本版本不自取）。
        `None` 与 `[]` 语义不同：`None` = 自取，`[]` = 显式空 —— 故 `None` **不上线**。

        `schema` / `options` 同纪律：`None` = 没指定，不上线（送出去等于让对面去猜
        「是没给，还是显式要空」）。

        `content_generation`（**代次 fencing，承重**）：提交那一刻的文档代次，落进任务行、
        由 `get_compile_status` 原样回显。KB 对账的 `_handle_completed` 拿它与
        `doc.content_generation` 比，小则判「旧代次的结果」，接管行在途时跳过 ——
        **文档会停在 EXTRACTING 永不 READY**。缺省 `None` = 不带该键 = 容器按 0 记
        （与 rag 链 `retry_ontology_extract(content_generation=0)` 同义）。`0` 是**合法值**
        （全新上传的文档就是 0），故这里「没给」与「给了 0」在容器侧同解 —— 与
        `schema`/`chunks` 那条「`None` 不上线」的纪律不冲突：那两处 `None` 与 `{}`/`[]`
        是两种不同意图，而这里两种取值落到容器是同一个数。
        """
        ...

    @abstractmethod
    async def get_compile_status(self, *, compile_id: str, tenant_id: str,
                                 include_data: bool = False) -> dict[str, Any]:
        """读编译状态。形状与旧任务（`retry_ontology_extract` 那一套）**保持不变**：

        `compile_id` / `status` / `stage` / `progress` / `ontology_status` /
        `ontology_error` / `content_generation`，`include_data=True` 时另带
        `ontology_data`。

        `content_generation` **不是可选附带信息**：KB 对账收尾的代次栅栏读它
        （见 `compile()` 的 `content_generation` 段）。缺键 ⇒ 0 ⇒ 被重新解析过的文档
        永远收不了尾，且不报错。

        按 `compile_id` 定位，**不需要 KB**（与同类 `get_task_status(*, task_id, tenant_id)` 同款）。

        `include_data=False` 是**常态**：`ontology_data` 是整份抽取结果（大），
        KB 对账 / 看门狗只读 `ontology_status` / `ontology_error` 三列，用不着它。
        """
        ...

    @abstractmethod
    async def retry_compile(self, *, compile_id: str, tenant_id: str,
                            force: bool = False) -> dict[str, Any]:
        """重投一次编译（对应旧 `retry_ontology_extract`）：任务置回 `queued`，返回同样的形状。

        ⚠️ `force` **当前容器侧没有消费者**（`capability.py::_retry` 只做「置回 queued」）。
        保留签名与上线（设计 §4.2 列为契约参数）是为了形状对齐，**调用方不要依赖它**；
        「强制」需要的话得先给 `_retry` 开门 —— 否则就是一个发得出去、没人读的静默参数。
        """
        ...

    async def close(self) -> None:
        """释放连接池。**非抽象**：MOCK 无资源可放，调用方（KB 容器）不必分模式分支。"""
        return None


class MockOntologyCompileClient(OntologyCompileClient):
    """测试桩（与既有 mock client 对齐）：返回成功形状，不碰网络。"""

    async def compile(self, **kwargs: Any) -> dict[str, Any]:
        return {"compile_id": "mock-compile", "status": "queued"}

    async def get_compile_status(self, *, compile_id: str, tenant_id: str,
                                 include_data: bool = False) -> dict[str, Any]:
        # 形状与真实现一致（`ontology_status` / `ontology_error` / `content_generation`
        # 也在）：KB 对账与看门狗直接按这几个键读数，桩少一个键就会把「读不到」演成
        # AttributeError（或代次栅栏恒按 0 判）。
        out: dict[str, Any] = {
            "compile_id": compile_id,
            "status": "completed",
            "stage": "done",
            "progress": 1.0,
            "ontology_status": "completed",
            "ontology_error": "",
            # 桩没有「提交时的代次」可回显 —— 0 是它的诚实取值（与缺键同解）。
            "content_generation": 0,
        }
        if include_data:
            out["ontology_data"] = {}
        return out

    async def retry_compile(self, *, compile_id: str, tenant_id: str,
                            force: bool = False) -> dict[str, Any]:
        return {"compile_id": compile_id, "status": "queued"}


class RemoteOntologyCompileClient(OntologyCompileClient):
    """经 Sidecar `/invoke` 调用能力容器（生产用）。"""

    def __init__(self, endpoint: str | None = None,
                 capability_id: str = CAPABILITY_ID) -> None:
        from jonex_core.common.config import get_config

        cfg = get_config()
        # [jonex] 内部鉴权必须用**配置的**网关 key：Sidecar 侧 `_gateway_api_key_matches`
        # 是精确比对 GATEWAY_API_KEY，任何硬编码字面量都会 401。jonex_wiki 曾因
        # 硬编码 "jonex_test_gateway" 导致检索全量失败，且被上层误报成
        # 「未找到相关信息」—— 同一个坑不要再踩。
        #
        # 超时 300s：**不是**给编译本身留的（三个 action 都短：入队 / 读状态 / 重投，
        # 真正的活在容器 worker 里）。给这么宽是防「请求落在正忙的容器上」：
        # worker 与 HTTP 服务同进程（B1），大文档抽取期间事件循环被占住时，
        # 一次入队也要排队。等待编译结果靠轮询 status，不靠拉长这次 POST。
        self._http = httpx.AsyncClient(
            base_url=(endpoint or cfg.SIDECAR_URL or "http://sidecar:8000").rstrip("/"),
            headers={"X-API-Key": cfg.GATEWAY_API_KEY},
            timeout=300,
        )
        self._capability_id = capability_id

    async def _invoke(self, action: str, data: dict[str, Any], tenant_id: str,
                      kb_id: str = "") -> dict:
        payload: dict[str, Any] = {"action": action, "data": data}
        if kb_id:
            # 能力侧 `_dispatch` 读 `payload.get("knowledge_base_id") or data.get("kb_id")`
            # ——kb_id 走 payload 顶层（平台既有约定，同 jonex_wiki）。
            # 取状态 / 重投不带 KB：它们按 compile_id 定位，多传一个无关参数只会多一处歧义。
            payload["knowledge_base_id"] = kb_id
        body = {
            "capability_id": self._capability_id,
            "payload": payload,
            "tenant_id": tenant_id,
        }
        resp = await self._http.post(
            "/invoke", json=body, headers={"X-Tenant-ID": tenant_id}
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 反代层直接回了非 2xx（鉴权 / 路由 / 限流 / 容器挂）—— 状态码翻成平台异常，
            # 别把 httpx 的异常类型漏给业务层。
            code = exc.response.status_code
            http_details: dict[str, Any] = {"action": action, "http_status": code}
            # 响应体摘要（键名照 Sidecar 的 `upstream_body`）：把「403 code=3004 鉴权失败」
            # 这类**可诊断**的信息留给调用方，而不是一句「HTTP 500」。
            body_summary = _response_body_summary(exc.response)
            if body_summary:
                http_details["upstream_body"] = body_summary
            message = f"ontology_compile 调用失败（HTTP {code}）：{action}"
            if code == 429:
                # 租户级限流：把「该退避重试」写进 message 与 details（含 `Retry-After`）。
                retry_after = (exc.response.headers.get("Retry-After") or "").strip()
                if retry_after:
                    http_details["retry_after"] = retry_after
                    message += f"，{retry_after}s 后重试"
                else:
                    message += "（触发租户级限流，请退避后重试）"
            raise _STATUS_FALLBACK_MAP.get(code, CapabilityInvokeError)(
                message=message,
                details=http_details,
            ) from exc
        result = resp.json()
        # 缺 `success` 键按**成功**解（见模块 docstring ②）：Sidecar 自己就是这个口径。
        if not result.get("success", True):
            message = str(result.get("message") or "ontology_compile invoke failed")
            try:
                code = int(result.get("code") or 0)
            except (TypeError, ValueError):
                code = 0
            env_details: dict[str, Any] = {"action": action, "code": code}
            # 信封里的 `details`（若有）带出来：Sidecar 的 `InvokeResult` 声明了该字段
            # （`sidecar/main.py:90`）且 `/invoke` 原样转发（`:843`），能力侧
            # `CapabilityResponse.error(..., details={...})` 放什么这里就能读到什么。
            upstream_details = result.get("details")
            if isinstance(upstream_details, dict) and upstream_details:
                env_details["upstream_details"] = upstream_details
            raise _STATUS_FALLBACK_MAP.get(code, CapabilityInvokeError)(
                message=message,
                details=env_details,
            )
        return result["data"]

    async def compile(self, *, document_id: str, knowledge_base_id: str, tenant_id: str,
                      mode: str = "ingest_extract", schema: dict[str, Any] | None = None,
                      chunks: list[dict[str, Any]] | None = None,
                      options: dict[str, Any] | None = None,
                      content_generation: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"document_id": document_id, "mode": mode}
        # 只带**实际给了值**的可选字段：`None` 上线路由成「对面显式置空」的歧义。
        # `chunks` 尤其致命：能力侧 `data.get("chunks")` 有值就不再自取（设计 §4.3），
        # 把 `None` 送出去等于在「自取」这条路上多一次「谁说了算」的猜谜。
        if schema is not None:
            data["schema"] = schema
        if chunks is not None:
            data["chunks"] = chunks
        if options is not None:
            data["options"] = options
        # `0` 是合法代次（全新上传）且要真的送出去，故判据是 `is not None` 而不是真值判断。
        if content_generation is not None:
            data["content_generation"] = content_generation
        return await self._invoke("compile", data, tenant_id, knowledge_base_id)

    async def get_compile_status(self, *, compile_id: str, tenant_id: str,
                                 include_data: bool = False) -> dict[str, Any]:
        return await self._invoke(
            "get_compile_status",
            {"compile_id": compile_id, "include_data": include_data},
            tenant_id,
        )

    async def retry_compile(self, *, compile_id: str, tenant_id: str,
                            force: bool = False) -> dict[str, Any]:
        # `force` 照契约上线（设计 §4.2），但容器侧当前不读它 —— 见抽象方法 docstring。
        return await self._invoke(
            "retry_compile",
            {"compile_id": compile_id, "force": force},
            tenant_id,
        )

    async def close(self) -> None:
        await self._http.aclose()


def get_ontology_compile_client(capability_id: str = CAPABILITY_ID) -> OntologyCompileClient:
    """按 `capability_runtime.yaml` 的声明取 client。

    只支持 MOCK / REMOTE（引擎与 worker 同进程，B1）。**LOCAL 显式报错而不是静默回落**：
    `get_locator()` 对未声明的能力默认返回 LOCAL（`jonex_core/capability/locator.py`），
    静默给个假 client 会让「本体编译」一路「成功」而什么都没发生 —— 而 KB 那边据此
    推进 `ontology_status`，是最难查的一类故障。
    """
    spec = get_locator().get_spec(capability_id)

    if spec.mode == CapabilityMode.MOCK:
        return MockOntologyCompileClient()

    if spec.mode == CapabilityMode.REMOTE:
        return RemoteOntologyCompileClient(
            endpoint=spec.endpoint, capability_id=capability_id
        )

    # `getattr(..., "value", ...)`：`spec.mode` 正常是 `CapabilityMode`，但清单里写了
    # 认不出的 mode 时 locator 已回落 LOCAL —— 报错文案不该在这条路径上再炸一次。
    mode = getattr(spec.mode, "value", spec.mode)
    raise OperationNotSupportedError(
        message=f"ontology_compile 不支持 {mode} 模式（引擎与 worker 同进程，B1）："
                f"{capability_id}",
        details={"capability_id": capability_id, "mode": mode},
    )
