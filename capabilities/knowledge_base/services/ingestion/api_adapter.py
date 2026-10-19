#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""API 出站拉取适配器（REST + JSON 列表，一期）。

安全（渗透报告 3.1 / RC-C，SSRF 严重）：本适配器是**用户可控出站目标**的执行点 ——

- ``cfg["endpoint"]`` 来自用户保存的数据源配置；
- ``rf.uri`` 来自**外部 API 响应体**（用户只控制 ``file_url_field`` 的字段名、不控制值），
  是报告的复查清单里没有的第二条 SSRF 路径。

两条地址都在**出站前**过 ``validate_kb_ingestion_url``（严格档：阻断私网 / 回环 /
链路本地 / 云元数据；白名单 ``KB_INGESTION_ALLOWED_HOSTS`` 是逃生阀）。保存时校验
不能替代出站时校验（DNS rebinding），故此处**再校验一次**。不跟随 30x 重定向
（``follow_redirects=False``），避免「起始地址合法、一个 302 跳到云元数据」。

另：``method`` 收紧为 ``{"GET", "POST"}``；``auth.header_name`` 限制为合法 token
字符集并拒绝危险名（Host / Content-Length / Transfer-Encoding / Connection 等）。
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from jonex_core.common.config import get_config
from jonex_core.common.crypto import decrypt_secret
from jonex_core.common.exceptions import InvalidParameterError, InvalidTargetUrlError
from jonex_core.common.i18n import translate
from jonex_core.security.outbound_url_validator import validate_kb_ingestion_url

from .base import RemoteFile

logger = logging.getLogger(__name__)

# 允许的出站 HTTP 方法。报告实测：现状接受任意字符串（``FOOBAR`` 也会出站）。
API_METHOD_WHITELIST = frozenset({"GET", "POST"})

# 危险请求头名（小写比较）：由 HTTP 客户端 / 传输层决定，用户指定会改变真实出站语义
# —— Host 可把请求指向别的主机；Content-Length / Transfer-Encoding / Connection
# 可造请求走私或绕过中间件策略。
DANGEROUS_HEADER_NAMES = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
})

# RFC 7230 token 字符集：合法 header 名只允许这些字符。
# ⚠️ 用 fullmatch（不是 match + ``$``）：``$`` 会匹配结尾换行，放行 ``"X-Evil\n"``。
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+\-.^_`|~]+")


def validate_api_config(cfg: dict[str, Any]) -> None:
    """校验 api 数据源配置里的 ``method`` 与 ``auth.header_name``（非法 → 1001）。

    保存路径（``DataSourceService._validate_endpoint``）与出站路径
    （``list_remote_files``）共用本函数：只做保存时校验挡不住改动前已落库的旧配置
    （如 ``method=FOOBAR``），出站前再校验一次才算真正封住。
    """
    method = cfg.get("method") or "GET"
    if not isinstance(method, str) or method.strip().upper() not in API_METHOD_WHITELIST:
        raise InvalidParameterError(message=translate(
            "err.datasource.invalid_method",
            fallback="不支持的请求方法，仅支持 GET / POST",
        ))

    auth = cfg.get("auth")
    if isinstance(auth, dict) and str(auth.get("type") or "").lower() == "api_key":
        header_name = auth.get("header_name")
        if header_name:
            # 不 strip：带首尾空白 / 换行的名字会被 fullmatch 拒绝（strip 后校验而
            # 出站用原值 = 校验形同虚设，还可能造成 header 注入）。
            name = str(header_name)
            if not _HEADER_NAME_RE.fullmatch(name) or name.lower() in DANGEROUS_HEADER_NAMES:
                raise InvalidParameterError(message=translate(
                    "err.datasource.invalid_header_name",
                    fallback="自定义请求头名称非法或不被允许",
                ))


def _safe_error_message(exc: Exception) -> str:
    """把出站异常映射为**固定文案**：不回传 ``str(exc)``（内含完整内网 URL、状态码
    甚至上游响应体）。细节只由调用方写 logger。
    """
    if isinstance(exc, InvalidTargetUrlError):
        return translate(
            "err.datasource.unsafe_endpoint",
            fallback="目标地址不被允许（禁止访问内网 / 回环 / 链路本地地址）",
        )
    if isinstance(exc, InvalidParameterError):
        return translate(
            "err.datasource.invalid_config",
            fallback="数据源配置非法，请检查请求方法与自定义请求头",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return translate(
            "err.datasource.upstream_status",
            fallback="目标接口返回异常状态，请检查地址与凭据配置",
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return translate(
            "err.datasource.connect_failed",
            fallback="无法连接目标接口，请检查地址与网络",
        )
    if isinstance(exc, httpx.TimeoutException):
        return translate("err.datasource.timeout", fallback="连接目标接口超时")
    return translate(
        "err.datasource.connect_failed",
        fallback="无法连接目标接口，请检查地址与网络",
    )


class ApiIngestionAdapter:
    """通过 REST 接口拉取外部 JSON 文档列表并下载。"""

    def _headers(self, cfg: dict[str, Any]) -> dict:
        auth = cfg.get("auth") or {}
        atype = (auth.get("type") or "none").lower()
        headers: dict[str, str] = {}
        token_ref = auth.get("token_ref", "")
        if atype == "bearer":
            headers["Authorization"] = f"Bearer {decrypt_secret(token_ref)}"
        elif atype == "api_key":
            headers[auth.get("header_name") or "X-API-Key"] = decrypt_secret(token_ref)
        elif atype == "basic":
            raw = decrypt_secret(token_ref)  # "user:pass"
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
        return headers

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            files = await self.list_remote_files(cfg)
            return {"ok": True, "message": translate("success.connection", fallback="连接成功"), "sample_count": len(files)}
        except Exception as e:  # noqa: BLE001
            # 脱敏：不回传 str(e)（内含完整内网 URL 与状态码），细节只进 logger。
            logger.warning("API 数据源测试连接失败: %s", e, exc_info=True)
            return {"ok": False, "message": _safe_error_message(e)}

    async def list_remote_files(
        self, cfg: dict[str, Any], since: Optional[datetime] = None,
    ) -> list[RemoteFile]:
        endpoint = cfg["endpoint"]
        # 出站前二次校验（防 TOCTOU / DNS rebinding）：保存时校验不能替代出站时校验。
        validate_kb_ingestion_url(
            endpoint, allowed_hosts=get_config().kb_ingestion_allowed_hosts
        )
        validate_api_config(cfg)
        method = (cfg.get("method") or "GET").strip().upper()
        list_path = cfg.get("list_path", "$.data.items")
        name_field = cfg.get("file_name_field", "name")
        url_field = cfg.get("file_url_field", "url")
        id_field = cfg.get("file_id_field", url_field)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(method, endpoint, headers=self._headers(cfg))
            resp.raise_for_status()
            body = resp.json()
        from jsonpath_ng import parse as jsonpath_parse

        matches = jsonpath_parse(list_path).find(body)
        items = matches[0].value if matches else []
        out: list[RemoteFile] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            uri = it.get(url_field)
            if not uri:
                continue
            out.append(
                RemoteFile(
                    external_id=str(it.get(id_field) or uri),
                    name=str(it.get(name_field) or str(uri).rsplit("/", 1)[-1]),
                    uri=str(uri),
                )
            )
        return out

    async def fetch_bytes(self, cfg: dict[str, Any], rf: RemoteFile) -> tuple[bytes, str]:
        # 二次 SSRF：rf.uri 来自外部 API 响应体，用户只控制字段名、不控制值。
        # 即使 endpoint 是公网，也必须逐条校验下载地址后再出站。
        validate_kb_ingestion_url(
            rf.uri, allowed_hosts=get_config().kb_ingestion_allowed_hosts
        )
        # 下载路径也会带 auth 头，method / header_name 同样在校验范围内。
        validate_api_config(cfg)
        # 不跟随 30x：起始地址合法也不能保证 redirect 目标合法（一个 302 就能跳到
        # 169.254.169.254）。与 identity_sync_client 的既有做法一致。
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            resp = await client.get(rf.uri, headers=self._headers(cfg))
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            return resp.content, mime


__all__ = [
    "ApiIngestionAdapter",
    "API_METHOD_WHITELIST",
    "DANGEROUS_HEADER_NAMES",
    "validate_api_config",
]
