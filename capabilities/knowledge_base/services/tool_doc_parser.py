#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""把 curl / OpenAPI 文本解析成工具草稿种子；解析阶段就剥离凭据。

只做确定性解析，不调用 LLM、不联网。返回值里的 redacted_text 才允许送模型；
seed 里的 runtime 不含 secret，Authorization / Cookie / token 等值不会保留。
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

import yaml

_SECRET_KEY_RE = re.compile(
    r"(?i)^(authorization|cookie|proxy-authorization|token|access_token|refresh_token|"
    r"api[_-]?key|secret|password|passwd|pwd)$"
)
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_SAFE_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _is_sensitive_name(name: Any) -> bool:
    """凭据字段名判定：精确名 + 常见前后缀（client_secret / X-Api-Key 等）。"""
    lower = str(name or "").strip().lower()
    if _SECRET_KEY_RE.match(lower):
        return True
    return any(
        marker in lower
        for marker in (
            "secret", "token", "password", "passwd", "apikey", "api_key",
            "api-key", "credential",
        )
    )


def _is_sensitive_header(name: Any) -> bool:
    """请求头判定：在通用凭据名之外，额外拦截 X-Auth / Authentication。
    不能把 'auth' 加进 _is_sensitive_name——runtime.auth 是结构字段，不是凭据。"""
    lower = str(name or "").strip().lower()
    return _is_sensitive_name(lower) or lower in (
        "auth", "x-auth", "authentication", "x-authentication",
    )


def redact_text(text: str) -> tuple[str, list[str]]:
    """把常见凭据从自由文本里打码；返回 (redacted, warnings)。"""
    warnings: list[str] = []
    out = text or ""

    def _header(match: re.Match) -> str:
        warnings.append("已剥离请求头中的凭据")
        return f"{match.group(1)}***"

    out = re.sub(
        r"(?im)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s\"',;]+",
        _header, out,
    )
    out = re.sub(r"(?im)(cookie\s*[:=]\s*)[^\n\r]+", _header, out)
    out = re.sub(
        r"(?im)((?:^|[\s'\"])(?:[A-Za-z0-9_.-]*(?:token|secret|password|passwd|pwd|apikey"
        r"|api[_-]?key|credential|auth))\s*[:=]\s*)[^\n\r'\"]+",
        _header, out,
    )

    def _curl_user(match: re.Match) -> str:
        warnings.append("已剥离 curl -u 的密码")
        return f"{match.group(1)}{match.group(2)}:***"

    out = re.sub(r"(?i)(\s-u\s+)([^:\s'\"]+):[^\s'\"]+", _curl_user, out)
    out = re.sub(r"(?i)(--user\s+)([^:\s'\"]+):[^\s'\"]+", _curl_user, out)
    out = re.sub(
        r"(?i)(([A-Za-z0-9_.-]*(?:token|secret|password|passwd|pwd|apikey|api[_-]?key"
        r"|credential))\s*[=:]\s*)([^\s&\"',;}]+)",
        lambda m: f"{m.group(1)}***", out,
    )
    out = re.sub(
        r'(?i)("(?:[^"]*(?:token|secret|password|passwd|pwd|apikey|api[_-]?key'
        r'|credential))"\s*:\s*")[^"]*(")',
        r"\1***\2", out,
    )
    return out, list(dict.fromkeys(warnings))


def _safe_tool_name(value: str, fallback: str = "new_tool") -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower())
    name = re.sub(r"_+", "_", name).strip("_")
    if not name or not name[0].isalpha():
        name = f"api_{name}".strip("_")
    if not _TOOL_NAME_RE.match(name):
        name = fallback
    return name[:128]


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _strip_secret_object(value: Any, warnings: list[str], depth: int = 0) -> Any:
    if depth > 5:
        return value
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if _is_sensitive_name(key):
                warnings.append(f"已移除敏感字段：{str(key)[:64]}")
                continue
            out[key] = _strip_secret_object(child, warnings, depth + 1)
        return out
    if isinstance(value, list):
        return [_strip_secret_object(item, warnings, depth + 1) for item in value[:50]]
    return value


def _auth_from_headers(headers: dict[str, str], warnings: list[str]) -> dict:
    auth = {"type": "none"}
    for key in list(headers.keys()):
        lower = key.lower()
        value = str(headers[key])
        if lower == "authorization":
            headers.pop(key, None)
            if value.lower().startswith("bearer "):
                auth = {"type": "bearer"}
                warnings.append("检测到 Bearer Authorization，已剥离值，请在鉴权设置里填写凭据")
            elif value.lower().startswith("basic "):
                auth = {"type": "basic", "username": "YOUR_USERNAME"}
                warnings.append("检测到 Basic Authorization，已剥离值；请补全用户名并填写凭据")
            else:
                warnings.append("检测到 Authorization 头，已剥离；请手动配置鉴权")
        elif lower in ("cookie", "proxy-authorization"):
            headers.pop(key, None)
            warnings.append(f"已剥离 {key} 请求头")
        elif lower == "x-api-key":
            headers.pop(key, None)
            auth = {"type": "api_key", "in": "header", "name": key}
            warnings.append("检测到 X-API-Key，已剥离值；请在鉴权设置里填写凭据")
        elif _is_sensitive_header(key):
            headers.pop(key, None)
            warnings.append(f"已剥离 {key} 请求头；请手动配置鉴权")
    return auth


def _parse_curl(text: str) -> dict:
    warnings: list[str] = []
    try:
        parts = shlex.split(text.replace("\\\r\n", " ").replace("\\\n", " "))
    except ValueError as exc:
        return {"kind": "text", "seed": None, "redacted_text": text, "warnings": [f"curl 解析失败：{exc}"]}
    if not parts or parts[0].lower() != "curl":
        return {"kind": "text", "seed": None, "redacted_text": text, "warnings": []}

    method = ""
    url = ""
    headers: dict[str, str] = {}
    data: Optional[str] = None
    basic_username = ""
    index = 1
    # 带参数的 curl flag；未知 flag 只跳过自身，避免把后面的 URL 当成参数吃掉
    arg_flags = {
        "-X", "--request", "-H", "--header", "-d", "--data", "--data-raw",
        "--data-binary", "--url", "-u", "--user", "-b", "--cookie", "-F", "--form",
        "--connect-timeout", "-m", "--max-time", "--retry", "--proxy", "-o", "--output",
    }
    while index < len(parts):
        part = parts[index]
        if part in ("-X", "--request") and index + 1 < len(parts):
            method = parts[index + 1].upper()
            index += 2
        elif part in ("-H", "--header") and index + 1 < len(parts):
            raw = parts[index + 1]
            if ":" in raw:
                key, value = raw.split(":", 1)
                headers[key.strip()] = value.strip()
            index += 2
        elif part in ("-d", "--data", "--data-raw", "--data-binary") and index + 1 < len(parts):
            data = parts[index + 1]
            index += 2
        elif part in ("--url",) and index + 1 < len(parts):
            url = parts[index + 1]
            index += 2
        elif part in ("-u", "--user") and index + 1 < len(parts):
            basic_username = parts[index + 1].split(":", 1)[0]
            warnings.append("已剥离 curl -u 的密码，请填写凭据")
            index += 2
        elif part in ("-b", "--cookie") and index + 1 < len(parts):
            warnings.append("已剥离 curl Cookie")
            index += 2
        elif part in ("-F", "--form") and index + 1 < len(parts):
            warnings.append("curl 表单上传（-F）暂不支持，请手动补全 body")
            index += 2
        elif part in arg_flags and index + 1 < len(parts):
            index += 2
        elif part.startswith("-"):
            index += 1
        else:
            if not url:
                url = part
            index += 1

    if not url:
        return {"kind": "curl", "seed": None, "redacted_text": redact_text(text)[0], "warnings": ["curl 里没有找到 URL"]}
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
        warnings.append("URL 缺少协议，草稿按 https 补全，请确认")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return {"kind": "curl", "seed": None, "redacted_text": redact_text(text)[0], "warnings": ["curl URL 不合法"]}

    query: dict = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_sensitive_name(key):
            warnings.append(f"已剥离 query 中的敏感参数：{key[:64]}")
            continue
        query[key] = f"{{{key}}}"
    if query:
        warnings.append("query 参数已改成 {参数} 占位符，请确认哪些需要暴露为工具入参")

    path_params = _PATH_PARAM_RE.findall(parsed.path)
    auth = _auth_from_headers(headers, warnings)
    if basic_username:
        auth = {"type": "basic", "username": basic_username[:128]}
    clean_headers = {
        key: value for key, value in headers.items()
        if _SAFE_HEADER_RE.match(key) and not _is_sensitive_header(key)
    }
    body: Any = None
    if data:
        try:
            body = json.loads(data)
            body = _strip_secret_object(body, warnings)
        except json.JSONDecodeError:
            warnings.append("请求体不是 JSON，运行时 body 仅支持 JSON 对象/数组，请手动补全")
    if not method:
        method = "POST" if data else "GET"

    input_props: dict = {}
    required: list[str] = []
    for name in path_params:
        input_props[name] = {"type": "string", "description": "路径参数"}
        required.append(name)
    for name in query:
        input_props[name] = {"type": "string", "description": "查询参数"}
    if isinstance(body, dict):
        for key, value in body.items():
            input_props.setdefault(str(key), {"type": _value_type(value)})

    last_segment = [segment for segment in parsed.path.split("/") if segment][-1:] or ["new_tool"]
    tool_name = _safe_tool_name(last_segment[0])
    # URL 里不带 query：query 统一走 runtime.query 占位符，避免把 token 等值留在 URL
    base_url = parsed._replace(query="", fragment="").geturl()
    runtime: dict = {"type": "api", "method": method, "url": base_url, "auth": auth, "timeout": 30}
    if query:
        runtime["query"] = query
    if clean_headers:
        runtime["headers"] = clean_headers
    if isinstance(body, (dict, list)):
        runtime["body"] = body

    seed = {
        "tool_name": tool_name,
        "name": tool_name.replace("_", " "),
        "description": "",
        "input_schema": {"type": "object", "properties": input_props, "required": required},
        "output_schema": {},
        "runtime": runtime,
    }
    redacted, redact_warnings = redact_text(text)
    warnings.extend(redact_warnings)
    return {"kind": "curl", "seed": seed, "redacted_text": redacted, "warnings": list(dict.fromkeys(warnings))}


def _resolve_ref(doc: dict, value: Any, depth: int = 0) -> Any:
    if depth > 5 or not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = str(value.get("$ref") or "")
    if not ref.startswith("#/"):
        return value
    node: Any = doc
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return value
        node = node.get(part.replace("~1", "/").replace("~0", "~"))
    return node


def _schema_to_input(schema: Any) -> tuple[dict, list[str]]:
    schema = schema if isinstance(schema, dict) else {}
    props: dict = {}
    required: list[str] = []
    raw_props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name, prop in raw_props.items():
        prop = prop if isinstance(prop, dict) else {}
        clean = {"type": str(prop.get("type") or "string")}
        if prop.get("description"):
            clean["description"] = str(prop["description"])[:300]
        props[str(name)] = clean
    for name in schema.get("required") or []:
        if str(name) in props:
            required.append(str(name))
    return {"type": "object", "properties": props, "required": required}, required


def _parse_openapi(doc: dict, text: str) -> dict:
    warnings: list[str] = []
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    operation = None
    method = ""
    path = ""
    for raw_path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for candidate in ("post", "get", "put", "patch", "delete"):
            if isinstance(item.get(candidate), dict):
                operation = item[candidate]
                method = candidate.upper()
                path = str(raw_path)
                break
        if operation is not None:
            break
    if operation is None:
        return {"kind": "openapi", "seed": None, "redacted_text": redact_text(text)[0], "warnings": ["OpenAPI 里没有找到可用 operation"]}
    if len(paths) > 1:
        warnings.append("OpenAPI 有多个 path，草稿取第一个可用 operation，请确认")

    servers = doc.get("servers") if isinstance(doc.get("servers"), list) else []
    base = ""
    if servers and isinstance(servers[0], dict):
        base = str(servers[0].get("url") or "").rstrip("/")
        variables = servers[0].get("variables") or {}
        if isinstance(variables, dict):
            for name, spec in variables.items():
                default = str((spec or {}).get("default") or "") if isinstance(spec, dict) else ""
                if default:
                    base = base.replace(f"{{{name}}}", default)
    url = f"{base}{path}" if base else ""
    if not url:
        warnings.append("OpenAPI 未提供 servers.url，请在草稿里补全调用地址")

    auth = {"type": "none"}
    schemes = ((doc.get("components") or {}).get("securitySchemes") or {}) if isinstance(doc.get("components"), dict) else {}
    security = operation.get("security") or doc.get("security") or []
    scheme_name = ""
    if security and isinstance(security[0], dict):
        scheme_name = next(iter(security[0].keys()), "")
    scheme = schemes.get(scheme_name) if isinstance(schemes, dict) and scheme_name else None
    if isinstance(scheme, dict):
        stype = str(scheme.get("type") or "").lower()
        if stype == "http" and str(scheme.get("scheme") or "").lower() == "basic":
            auth = {"type": "basic", "username": "YOUR_USERNAME"}
            warnings.append("OpenAPI 声明了 basic 鉴权，请补全用户名并填写凭据")
        elif stype == "http" or stype == "oauth2" or stype == "openidconnect":
            auth = {"type": "bearer"}
            warnings.append("OpenAPI 声明了 Bearer/OAuth 鉴权，请填写凭据")
        elif stype == "apikey":
            auth = {
                "type": "api_key",
                "in": str(scheme.get("in") or "header"),
                "name": str(scheme.get("name") or "X-API-Key"),
            }
            warnings.append("OpenAPI 声明了 API Key 鉴权，请填写凭据")

    input_props: dict = {}
    required: list[str] = []
    query: dict = {}
    for param in operation.get("parameters") or []:
        param = _resolve_ref(doc, param)
        if not isinstance(param, dict):
            continue
        name = str(param.get("name") or "").strip()
        where = str(param.get("in") or "").strip()
        if not name or where in ("header", "cookie"):
            continue
        prop = {"type": str((param.get("schema") or {}).get("type") or "string")}
        if param.get("description"):
            prop["description"] = str(param["description"])[:300]
        input_props[name] = prop
        if param.get("required"):
            required.append(name)
        if where == "query":
            query[name] = f"{{{name}}}"

    body_schema, body_required = {}, []
    request_body = _resolve_ref(doc, operation.get("requestBody"))
    if isinstance(request_body, dict):
        content = request_body.get("content") or {}
        payload = content.get("application/json") if isinstance(content, dict) else None
        if isinstance(payload, dict):
            body_schema, body_required = _schema_to_input(payload.get("schema"))
            for name, prop in (body_schema.get("properties") or {}).items():
                input_props.setdefault(name, prop)
            required.extend(name for name in body_required if name not in required)

    output_schema: dict = {}
    responses = operation.get("responses") or {}
    response = responses.get("200") or responses.get("201") or responses.get("default")
    if isinstance(response, dict):
        content = response.get("content") or {}
        payload = content.get("application/json") if isinstance(content, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("schema"), dict):
            output_schema = payload["schema"]

    op_id = str(operation.get("operationId") or "").strip()
    tool_name = _safe_tool_name(op_id or f"{method.lower()}_{path.strip('/').replace('/', '_')}")
    name = str(operation.get("summary") or operation.get("description") or tool_name)[:255]
    runtime: dict = {"type": "api", "method": method, "url": url, "auth": auth, "timeout": 30}
    if query:
        runtime["query"] = query
    seed = {
        "tool_name": tool_name,
        "name": name,
        "description": str(operation.get("description") or "")[:1000],
        "input_schema": {"type": "object", "properties": input_props, "required": required},
        "output_schema": output_schema,
        "runtime": runtime,
    }
    redacted, redact_warnings = redact_text(text)
    warnings.extend(redact_warnings)
    return {"kind": "openapi", "seed": seed, "redacted_text": redacted, "warnings": list(dict.fromkeys(warnings))}


def parse_tool_input(text: str) -> dict:
    """识别 curl / OpenAPI；其余返回 text，交给 LLM 自由生成。"""
    raw = (text or "").strip()
    if not raw:
        return {"kind": "text", "seed": None, "redacted_text": "", "warnings": []}
    if raw.lower().startswith("curl "):
        return _parse_curl(raw)
    if "openapi" in raw.lower() or "swagger" in raw.lower() or '"paths"' in raw:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            try:
                doc = yaml.safe_load(raw)
            except yaml.YAMLError:
                doc = None
        if isinstance(doc, dict) and isinstance(doc.get("paths"), dict):
            return _parse_openapi(doc, raw)
    redacted, warnings = redact_text(raw)
    return {"kind": "text", "seed": None, "redacted_text": redacted, "warnings": warnings}


__all__ = ["parse_tool_input", "redact_text"]
