"""标准资产字段与 payload 校验（W2.2）。"""
from __future__ import annotations

import json
import re

from capabilities.knowledge_base.services.metrics.executable import (
    ADDITIVITY_VALUES,
    normalize_executable,
    validate_executable,
)
from jonex_core.common.exceptions import InvalidParameterError

KINDS = frozenset({"term", "data_element", "metric_card"})
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_PAYLOAD_BYTES = 64 * 1024

_ALLOWED = {
    "term": {"definition", "aliases", "examples", "domain"},
    "data_element": {"definition", "data_type", "unit", "value_domain", "aliases"},
    # W3.3：additivity / executable 是**可选**增量；旧 payload 不传时出参逐字节不变。
    "metric_card": {"definition", "formula", "unit", "dimensions", "source",
                    "additivity", "executable"},
}
# 必填字段用元组且按字段名排序：报错取第一个缺失项，顺序不得随进程哈希种子漂移。
_REQUIRED = {
    "term": ("definition",),
    "data_element": ("data_type", "definition"),
    "metric_card": ("definition", "formula", "unit"),
}
_DATA_TYPES = frozenset({"string", "number", "date", "boolean", "json"})


def _bad(message: str, **details):
    raise InvalidParameterError(message=message, details=details or None)


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _bad(f"{field} 必须是非空字符串", field=field)
    if "\x00" in value:
        # NUL 字符无法写入 PostgreSQL 文本列（asyncpg 直接拒绝）。
        _bad(f"{field} 含非法字符（NUL）", field=field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理字符（\ud800-\udfff）无法编码，若放行会在下游 json/encode 炸成 500
        _bad(f"{field} 含非法字符（孤立代理项）", field=field)
    return value.strip()


def _text_list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        _bad(f"{field} 必须是数组", field=field)
    out: list[str] = []
    for item in value:
        text = _text(item, field)
        if text not in out:
            out.append(text)
    return out


def validate_asset_fields(kind: str, code: str, name: str) -> tuple[str, str, str]:
    if kind not in KINDS:
        _bad("kind 必须是 term/data_element/metric_card", kind=kind)
    code = _text(code, "code")
    if not _CODE_RE.match(code):
        _bad("code 必须以字母开头，只含字母数字 _ . -，长度 ≤128", code=code)
    name = _text(name, "name")
    if len(name) > 255:
        _bad("name 超长（>255）", name=name)
    return code, name, kind


def validate_payload(kind: str, payload) -> dict:
    # kind 先于 payload 形状校验：非法 kind 必须 1001，不得走到 _ALLOWED[kind] 抛 KeyError
    if kind not in KINDS:
        _bad("kind 必须是 term/data_element/metric_card", kind=kind)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        _bad("payload 必须是对象")
    unknown = set(payload) - _ALLOWED[kind]
    if unknown:
        _bad("payload 含未知字段", fields=sorted(unknown))
    for key in _REQUIRED[kind]:
        if key not in payload or payload[key] is None:
            _bad(f"payload 缺少必填字段 {key}", field=key)
    out = dict(payload)
    out["definition"] = _text(out["definition"], "definition")
    # 可选标量（domain/unit/source）显式传 null 与不传同义：从出参剔除，
    # 与 aliases/examples/value_domain/dimensions 的 null→[] 归一保持一致，
    # 返回 payload 不携带 None 值。
    if kind == "term":
        out["aliases"] = _text_list(out.get("aliases"), "aliases")
        out["examples"] = _text_list(out.get("examples"), "examples")
        if out.get("domain") is None:
            out.pop("domain", None)
        else:
            out["domain"] = _text(out["domain"], "domain")
    elif kind == "data_element":
        if isinstance(out["data_type"], str):
            out["data_type"] = out["data_type"].strip()
        if not isinstance(out["data_type"], str) or out["data_type"] not in _DATA_TYPES:
            # 先判类型：list/dict 无哈希，直接 in 会 TypeError 穿透
            _bad("data_type 非法", data_type=out["data_type"])
        out["aliases"] = _text_list(out.get("aliases"), "aliases")
        out["value_domain"] = _text_list(out.get("value_domain"), "value_domain")
        if out.get("unit") is None:
            out.pop("unit", None)
        else:
            out["unit"] = _text(out["unit"], "unit")
    elif kind == "metric_card":
        out["formula"] = _text(out["formula"], "formula")
        out["unit"] = _text(out["unit"], "unit")
        out["dimensions"] = _text_list(out.get("dimensions"), "dimensions")
        if out.get("source") is None:
            out.pop("source", None)
        else:
            out["source"] = _text(out["source"], "source")
        # W3.3：可选增量。缺省**不物化**（旧卡出参逐字节不变）；显式 null 等同不传。
        if out.get("additivity") is None:
            out.pop("additivity", None)
        else:
            additivity = _text(out["additivity"], "additivity").lower()
            if additivity not in ADDITIVITY_VALUES:
                _bad("additivity 非法", field="additivity",
                     allowed=list(ADDITIVITY_VALUES))
            out["additivity"] = additivity
        if out.get("executable") is None:
            out.pop("executable", None)
        else:
            executable = out["executable"]
            if not isinstance(executable, dict):
                _bad("executable 必须是对象", field="executable")
            # 结构校验与抽象侧共用（metrics.executable）；口径卡的输入指标存在性
            # 由 M3 的校验器补（那里才有租户内的 metric_card 作用域）。
            issues = validate_executable(executable, additivity=out.get("additivity"))
            if issues:
                _bad("executable 非法", field="executable", issues=issues[:20])
            out["executable"] = normalize_executable(executable)
    else:  # 防御：入口已校验 kind，正常不可达
        _bad("kind 必须是 term/data_element/metric_card", kind=kind)
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        _bad("payload 超长（>64KB）")
    return out
