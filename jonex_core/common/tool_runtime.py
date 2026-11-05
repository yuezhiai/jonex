#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""可执行工具的运行时：配置校验 + 调用。

平台工具目录（business_domain.skill_catalog.capability_json.runtime）与知识库工具
（knowledge_base.kb_tools.runtime）共用这一份，保证「保存时怎么校验」和「执行时怎么调用」
在两处完全一致。

运行时类型
    {"type": "echo"}   回显入参，联调用。可选 `mock`：`{"by": 入参名, "rows": {值: 数据}, "default"?}`，
                       按入参查表把数据放进输出的 `data`（没有真实接口时演示主数据查询）。
    {"type": "api", ...} 调用一个普通 HTTP API：
        method    GET / POST / PUT / PATCH / DELETE（缺省 POST）
        url       http(s) 地址；路径里可以写 `{参数}` 占位，取工具入参并做 URL 编码。
                  主机名里不允许占位 —— 出站目标必须在保存时就能做安全校验。
        query     查询参数模板 `{"名": "值或 {参数}"}`；整值是单个占位且入参为空时该参数不发。
        headers   固定请求头（同样支持占位）。Authorization / Cookie 不许写在这里：
                  凭据只能走 auth + 加密存储的 secret，不能明文落库。
        body      JSON 请求体模板；字符串值恰好是 `{参数}` 时按原类型代入（数字仍是数字），
                  部分占位按字符串拼接；值为空的键整体去掉。
                  不配 body 时：POST / PUT / PATCH 把（未用在路径里的）全部入参作为 JSON 体；
                  不配 query 时：GET / DELETE 把标量入参作为查询参数。
        auth      {"type": "none"}
                  {"type": "bearer"}                                 Authorization: Bearer <secret>
                  {"type": "api_key", "in": "header"|"query", "name": "X-API-Key"}
                  {"type": "basic", "username": "..."}               密码 = secret
                  secret 不在运行时配置里，由调用方从加密列取出解密后传入 `run_runtime`。
        response  {"extract": {"字段": "$.data.status"}} 用点路径从 JSON 响应里取字段
                  （支持 `.键` 与 `[下标]`）；不配时原样返回响应体。
        timeout   秒，(0, 30]，缺省 10。

安全
    - 出站地址按 `validate_target_url` 严格档校验（私网 / 回环 / 链路本地 / 云元数据一律拒绝，
      http 与 https 都接受）；内网工具需运维在 ONTOLOGY_TOOL_RUNTIME_ALLOWED_HOSTS 里
      显式登记主机名。保存时校验模板地址，执行时再校验渲染后的地址。
    - 不跟随重定向（重定向目标可能指向内网）。
    - 响应体超过 1 MB 截断。
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

from jonex_core.common.exceptions import InvalidParameterError, JonexException
from jonex_core.common.i18n import translate
from jonex_core.security.outbound_url_validator import validate_target_url

RUNTIME_ECHO = "echo"
RUNTIME_API = "api"
RUNTIME_TYPES = (RUNTIME_ECHO, RUNTIME_API)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
AUTH_TYPES = ("none", "bearer", "api_key", "basic")
ALLOWED_HOSTS_ENV = "ONTOLOGY_TOOL_RUNTIME_ALLOWED_HOSTS"

MAX_TIMEOUT = 30.0
DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1_000_000
_MAX_ENTRIES = 30
_MAX_BODY_CHARS = 16_000
_MAX_EXTRACT = 20

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,127})\}")
_FULL_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]{0,127})\}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$")
_QUERY_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,128}$")
_FORBIDDEN_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie",
    "host", "content-length", "transfer-encoding", "connection",
})


class MissingToolArgument(InvalidParameterError):
    """路径占位所需的入参缺失：调用方按「入参不全」处理，不发请求。"""

    def __init__(self, names: list[str]):
        super().__init__(
            message=translate(
                "err.tool_runtime.missing_argument", params={"names": ", ".join(names)},
                fallback=f"缺少调用地址所需的入参：{', '.join(names)}",
            ),
            details={"missing": names},
        )
        self.missing = names


def runtime_allowed_hosts() -> list[str]:
    """出站白名单（保存校验与执行共用同一口径）。"""
    raw = os.getenv(ALLOWED_HOSTS_ENV, "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _bad(reason: str) -> InvalidParameterError:
    return InvalidParameterError(message=translate(
        "err.tool_runtime.invalid", params={"reason": reason},
        fallback=f"运行时配置不合法：{reason}",
    ))


def _unsafe() -> InvalidParameterError:
    return InvalidParameterError(message=translate(
        "err.tool_runtime.unsafe_url",
        fallback=(
            "工具地址不安全：只允许 http/https，且不能指向私网 / 回环 / 链路本地地址；"
            "内网工具请让运维把主机名加入 ONTOLOGY_TOOL_RUNTIME_ALLOWED_HOSTS"
        ),
    ))


def _check_url(url: str) -> None:
    try:
        validate_target_url(url, allowed_hosts=runtime_allowed_hosts(), allow_http=True)
    except JonexException:
        raise _unsafe()


# ── 配置校验 ─────────────────────────────────────────────────────────


def _str_map(value: Any, field: str, name_re: re.Pattern) -> dict:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise _bad(f"{field} 必须是对象")
    if len(value) > _MAX_ENTRIES:
        raise _bad(f"{field} 最多 {_MAX_ENTRIES} 项")
    out = {}
    for k, v in value.items():
        k = str(k).strip()
        if not name_re.match(k):
            raise _bad(f"{field} 的名称不合法：{k[:40]}")
        if not isinstance(v, (str, int, float, bool)):
            raise _bad(f"{field}.{k} 只能是字符串、数字或布尔")
        out[k] = v if isinstance(v, str) else json.dumps(v)
    return out


def _check_extract_path(path: str) -> bool:
    if not path or len(path) > 256:
        return False
    return bool(re.fullmatch(r"\$?(\.?[^.\[\]\s]+|\[\d+\])*", path)) and path not in (".",)


_MAX_MOCK_ROWS = 200
_MAX_MOCK_CHARS = 32_000


def _validate_echo_mock(mock: Any) -> Optional[dict]:
    """echo 的模拟查表（联调用）：`{"by": 入参名, "rows": {入参值: 返回数据}, "default"?: 返回数据}`。

    调用时按 `arguments[by]` 查 `rows`，查不到用 `default`（没配为 null），结果放在输出的 `data` 里。
    用来在没有真实接口时演示「按编码 / 名称查主数据」这类工具。
    """
    if mock in (None, "", {}):
        return None
    if not isinstance(mock, dict):
        raise _bad("mock 必须是对象")
    by = str(mock.get("by") or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", by):
        raise _bad("mock.by 必须是入参名")
    rows = mock.get("rows")
    if not isinstance(rows, dict) or not rows:
        raise _bad("mock.rows 必须是非空对象：{入参值: 返回数据}")
    if len(rows) > _MAX_MOCK_ROWS:
        raise _bad(f"mock.rows 最多 {_MAX_MOCK_ROWS} 项")
    out: dict = {"by": by, "rows": {str(k): v for k, v in rows.items()}}
    if "default" in mock:
        out["default"] = mock["default"]
    if len(json.dumps(out, ensure_ascii=False)) > _MAX_MOCK_CHARS:
        raise _bad(f"mock 不超过 {_MAX_MOCK_CHARS} 字符")
    return out


def _echo_output(runtime: dict, arguments: dict, context: dict) -> dict:
    out: dict = {"echo": arguments, "tool_name": context.get("tool_name")}
    mock = runtime.get("mock")
    if isinstance(mock, dict) and mock.get("by"):
        key = arguments.get(mock["by"])
        rows = mock.get("rows") or {}
        out["data"] = rows.get(str(key)) if key is not None and str(key) in rows else mock.get("default")
    return out


def validate_runtime(runtime: Any) -> Optional[dict]:
    """校验并规整运行时配置；None / {} 表示没有可调入口。"""
    if runtime in (None, {}, ""):
        return None
    if not isinstance(runtime, dict):
        raise _bad("必须是对象")
    rtype = str(runtime.get("type") or "").strip()
    if rtype not in RUNTIME_TYPES:
        raise _bad(f"type 只能是 {'/'.join(RUNTIME_TYPES)}")
    if rtype == RUNTIME_ECHO:
        out_echo: dict = {"type": RUNTIME_ECHO}
        mock = _validate_echo_mock(runtime.get("mock"))
        if mock:
            out_echo["mock"] = mock
        return out_echo

    method = str(runtime.get("method") or "POST").strip().upper()
    if method not in HTTP_METHODS:
        raise _bad(f"method 只能是 {'/'.join(HTTP_METHODS)}")

    url = str(runtime.get("url") or "").strip()
    if not url:
        raise _bad("必须填写调用地址 url")
    parsed = urlparse(_PLACEHOLDER_RE.sub("x", url))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise _unsafe()
    if "{" in (urlparse(url).netloc or ""):
        raise _bad("主机名里不能使用 {参数} 占位")
    _check_url(_PLACEHOLDER_RE.sub("x", url))

    out: dict = {"type": RUNTIME_API, "method": method, "url": url}

    query = _str_map(runtime.get("query"), "query", _QUERY_NAME_RE)
    if query:
        out["query"] = query
    headers = _str_map(runtime.get("headers"), "headers", _HEADER_NAME_RE)
    bad_headers = [h for h in headers if h.lower() in _FORBIDDEN_HEADERS]
    if bad_headers:
        raise _bad(f"请求头 {bad_headers[0]} 不能直接配置，凭据请用鉴权设置")
    if headers:
        out["headers"] = headers

    body = runtime.get("body")
    if body not in (None, ""):
        if not isinstance(body, (dict, list)):
            raise _bad("body 必须是 JSON 对象或数组")
        if len(json.dumps(body, ensure_ascii=False)) > _MAX_BODY_CHARS:
            raise _bad(f"body 不超过 {_MAX_BODY_CHARS} 字符")
        out["body"] = body

    auth = runtime.get("auth") or {"type": "none"}
    if not isinstance(auth, dict):
        raise _bad("auth 必须是对象")
    atype = str(auth.get("type") or "none").strip()
    if atype not in AUTH_TYPES:
        raise _bad(f"auth.type 只能是 {'/'.join(AUTH_TYPES)}")
    norm_auth: dict = {"type": atype}
    if atype == "api_key":
        where = str(auth.get("in") or "header").strip()
        if where not in ("header", "query"):
            raise _bad("auth.in 只能是 header 或 query")
        name = str(auth.get("name") or ("X-API-Key" if where == "header" else "api_key")).strip()
        name_re = _HEADER_NAME_RE if where == "header" else _QUERY_NAME_RE
        if not name_re.match(name) or (where == "header" and name.lower() in _FORBIDDEN_HEADERS - {"authorization"}):
            raise _bad(f"auth.name 不合法：{name[:40]}")
        norm_auth.update({"in": where, "name": name})
    elif atype == "basic":
        username = str(auth.get("username") or "").strip()
        if not username or len(username) > 128:
            raise _bad("basic 鉴权必须填写用户名（不超过 128 字）")
        norm_auth["username"] = username
    out["auth"] = norm_auth

    resp = runtime.get("response") or {}
    if not isinstance(resp, dict):
        raise _bad("response 必须是对象")
    extract = resp.get("extract") or {}
    if extract:
        if not isinstance(extract, dict) or len(extract) > _MAX_EXTRACT:
            raise _bad(f"response.extract 必须是对象，最多 {_MAX_EXTRACT} 项")
        clean = {}
        for k, p in extract.items():
            k = str(k).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k):
                raise _bad(f"response.extract 的字段名不合法：{k[:40]}")
            if not isinstance(p, str) or not _check_extract_path(p.strip()):
                raise _bad(f"response.extract.{k} 的路径不合法")
            clean[k] = p.strip()
        out["response"] = {"extract": clean}

    if runtime.get("timeout") not in (None, ""):
        try:
            timeout = float(runtime.get("timeout"))
        except (TypeError, ValueError):
            timeout = -1
        if not (0 < timeout <= MAX_TIMEOUT):
            raise _bad(f"timeout 必须在 (0, {int(MAX_TIMEOUT)}] 秒")
        out["timeout"] = timeout
    return out


def _schema_err(field: str, reason: str) -> InvalidParameterError:
    return InvalidParameterError(message=translate(
        "err.tool_runtime.invalid_schema", params={"field": field, "reason": reason},
        fallback=f"{field} 不合法：{reason}",
    ))


def validate_tool_schema(schema: Any, field: str) -> dict:
    """工具的入参 / 出参 schema：至少是 JSON Schema object（properties 是对象，required 是已声明的字符串）。

    入参 schema 必须 `type: object`（工具绑定的参数映射、执行器的必填校验都按它来）；
    出参 schema 只做结构检查，空 = `{}`。
    """
    if schema in (None, {}):
        return {"type": "object", "properties": {}} if field == "input_schema" else {}
    if not isinstance(schema, dict):
        raise _schema_err(field, "必须是 JSON 对象")
    if field == "input_schema" and schema.get("type") != "object":
        raise _schema_err(field, "type 必须是 object")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise _schema_err(field, "properties 必须是对象")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise _schema_err(field, "required 必须是字符串数组")
    missing = [r for r in required if r not in props]
    if missing:
        raise _schema_err(field, f"required 引用了未声明的入参 {missing[0]}")
    return schema


def missing_required(input_schema: Optional[dict], arguments: dict) -> list[str]:
    """按入参 schema 的 required 找出缺失（None / 空串都算缺）。"""
    required = [str(r) for r in ((input_schema or {}).get("required") or [])]
    return [r for r in required if (arguments or {}).get(r) in (None, "")]


def requires_secret(runtime: Optional[dict]) -> bool:
    """该运行时调用时是否需要凭据。"""
    return bool(runtime) and runtime.get("type") == RUNTIME_API and \
        (runtime.get("auth") or {}).get("type", "none") != "none"


# ── 模板渲染 ─────────────────────────────────────────────────────────


class _Missing(Exception):
    def __init__(self, name: str):
        self.name = name


def _render_text(template: str, args: dict, *, encode: bool = False) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        value = args.get(name)
        if value in (None, ""):
            raise _Missing(name)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return quote(text, safe="") if encode else text
    return _PLACEHOLDER_RE.sub(repl, template)


def _render_json(template: Any, args: dict) -> Any:
    """渲染 JSON 模板；返回 `_DROP` 表示该值整体去掉。"""
    if isinstance(template, str):
        full = _FULL_PLACEHOLDER_RE.match(template)
        if full:
            value = args.get(full.group(1))
            return _DROP if value is None else value
        try:
            return _render_text(template, args)
        except _Missing:
            return _DROP
    if isinstance(template, dict):
        out = {}
        for k, v in template.items():
            r = _render_json(v, args)
            if r is not _DROP:
                out[k] = r
        return out
    if isinstance(template, list):
        return [r for r in (_render_json(v, args) for v in template) if r is not _DROP]
    return template


_DROP = object()


def _used_in(template: str) -> set:
    return set(_PLACEHOLDER_RE.findall(template or ""))


def build_request(runtime: dict, arguments: dict, secret: str = "") -> dict:
    """把运行时配置 + 入参渲染成一次请求：`{method, url, params, headers, json, auth}`。"""
    args = dict(arguments or {})
    url_tpl = runtime["url"]
    path_names = _used_in(url_tpl)
    missing = sorted(n for n in path_names if args.get(n) in (None, ""))
    if missing:
        raise MissingToolArgument(missing)
    url = _render_text(url_tpl, args, encode=True)
    method = runtime.get("method", "POST")

    params: dict = {}
    if "query" in runtime:
        for k, tpl in runtime["query"].items():
            try:
                params[k] = _render_text(tpl, args) if isinstance(tpl, str) else tpl
            except _Missing:
                continue
    elif method in ("GET", "DELETE"):
        for k, v in args.items():
            if k in path_names or v is None or isinstance(v, (dict, list)):
                continue
            params[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    headers: dict = {}
    for k, tpl in (runtime.get("headers") or {}).items():
        try:
            headers[k] = _render_text(tpl, args)
        except _Missing:
            continue

    body: Any = None
    if method in ("POST", "PUT", "PATCH"):
        if "body" in runtime:
            body = _render_json(runtime["body"], args)
            body = None if body is _DROP else body
        else:
            body = {k: v for k, v in args.items() if k not in path_names}

    auth = runtime.get("auth") or {"type": "none"}
    atype = auth.get("type", "none")
    if atype == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif atype == "api_key":
        if auth.get("in") == "query":
            params[auth.get("name") or "api_key"] = secret
        else:
            headers[auth.get("name") or "X-API-Key"] = secret
    elif atype == "basic":
        token = base64.b64encode(f"{auth.get('username', '')}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return {"method": method, "url": url, "params": params, "headers": headers, "json": body}


# ── 响应提取 ─────────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


def extract_path(payload: Any, path: str) -> Any:
    """点路径取值：`$.data.items[0].status` / `data.status`；取不到返回 None。"""
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    cur = payload
    for m in _TOKEN_RE.finditer(p):
        key, idx = m.group(1), m.group(2)
        if idx is not None:
            if not isinstance(cur, list) or int(idx) >= len(cur):
                return None
            cur = cur[int(idx)]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
    return cur


# ── 调用 ─────────────────────────────────────────────────────────────


async def run_runtime(
    runtime: dict, arguments: dict, *, secret: str = "", context: Optional[dict] = None,
    timeout_cap: Optional[float] = None,
) -> dict:
    """执行一次工具调用，返回 `{"ok", "output", "http_status"?, "error"?}`。

    `timeout_cap`：调用方的超时上限（秒），与工具自身的 `timeout` 取较小值
    （问答场景要比执行场景短得多，不另设一个工具字段）。

    抛出：`MissingToolArgument`（路径占位缺入参）、`InvalidParameterError`（地址不安全 /
    缺凭据）、`httpx.HTTPError`（网络 / 超时）—— 由调用方转成各自的执行状态。
    """
    context = context or {}
    rtype = (runtime or {}).get("type")
    if rtype == RUNTIME_ECHO:
        return {"ok": True, "output": _echo_output(runtime, arguments, context)}
    if rtype != RUNTIME_API:
        raise _bad("没有可调用的运行时")
    if requires_secret(runtime) and not secret:
        raise InvalidParameterError(message=translate(
            "err.tool_runtime.missing_secret", fallback="工具配置了鉴权，但还没有设置凭据",
        ))

    req = build_request(runtime, arguments, secret)
    _check_url(req["url"])
    try:
        timeout = min(float(runtime.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    if timeout_cap:
        timeout = min(timeout, float(timeout_cap))
    headers = dict(req["headers"])
    if context.get("request_id"):
        headers.setdefault("X-Request-ID", str(context["request_id"]))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)), follow_redirects=False,
    ) as client:
        resp = await client.request(
            req["method"], req["url"], params=req["params"] or None,
            headers=headers, json=req["json"] if req["json"] is not None else None,
        )
    raw = resp.content[:MAX_RESPONSE_BYTES]
    try:
        payload: Any = json.loads(raw.decode(resp.encoding or "utf-8", errors="replace"))
    except ValueError:
        payload = raw.decode(resp.encoding or "utf-8", errors="replace")
    if resp.status_code >= 400:
        return {"ok": False, "http_status": resp.status_code, "output": payload}
    extract = ((runtime.get("response") or {}).get("extract")) or {}
    if extract:
        payload = {k: extract_path(payload, p) for k, p in extract.items()}
    return {"ok": True, "http_status": resp.status_code, "output": payload}


def runtime_brief(runtime: Optional[dict]) -> Optional[dict]:
    """列表展示用的摘要（不含模板细节）。"""
    if not runtime:
        return None
    if runtime.get("type") == RUNTIME_API:
        return {
            "type": RUNTIME_API,
            "method": runtime.get("method"),
            "url": runtime.get("url"),
            "auth_type": (runtime.get("auth") or {}).get("type", "none"),
        }
    return {"type": runtime.get("type")}


__all__ = [
    "ALLOWED_HOSTS_ENV",
    "AUTH_TYPES",
    "HTTP_METHODS",
    "MAX_TIMEOUT",
    "MissingToolArgument",
    "RUNTIME_API",
    "RUNTIME_ECHO",
    "RUNTIME_TYPES",
    "build_request",
    "extract_path",
    "missing_required",
    "requires_secret",
    "run_runtime",
    "runtime_allowed_hosts",
    "runtime_brief",
    "validate_runtime",
    "validate_tool_schema",
]
