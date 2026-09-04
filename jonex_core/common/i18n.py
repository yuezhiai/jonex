#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 国际化（i18n）核心模块

提供：
- LocaleContext：基于 contextvars 的异步安全 locale 上下文
- extract_locale：按优先级解析请求 locale（X-Lang > DEFAULT_LOCALE）
- translate：多级回落翻译（locale → DEFAULT_LOCALE → fallback → str(code)）
- install_locale_middleware：FastAPI locale 中间件
- transmit_locale_header：透传 locale 到下游转发 headers
"""

from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from jonex_core.common.config import get_config

_MESSAGES_DIR = Path(__file__).parent / "messages"
_locale_ctx: ContextVar[Optional[str]] = ContextVar("jonex_locale", default=None)


class LocaleContext:
    """基于 contextvars 的异步安全 locale 上下文，与 TenantContext 同构。"""

    @classmethod
    def set(cls, locale: Optional[str]) -> Token:
        return _locale_ctx.set(locale or None)

    @classmethod
    def get(cls) -> Optional[str]:
        return _locale_ctx.get()

    @classmethod
    def reset(cls, token: Token) -> None:
        _locale_ctx.reset(token)

    @classmethod
    def clear(cls) -> Token:
        return _locale_ctx.set(None)


def normalize_locale(value: Optional[str]) -> Optional[str]:
    """归一化 locale 字符串，非法或不在 SUPPORTED_LOCALES 返回 None。

    先做长度截断保护（超过 LOCALE_MAX_LENGTH 直接拒绝），再大小写不敏感匹配。
    """
    if not value:
        return None
    cfg = get_config()
    v = value.strip()
    if len(v) > cfg.LOCALE_MAX_LENGTH:
        return None
    for sup in cfg.supported_locales_list:
        if sup.lower() == v.lower():
            return sup
    return None


def get_default_locale() -> str:
    return get_config().DEFAULT_LOCALE


def extract_locale(request) -> str:
    """按优先级解析请求 locale：X-Lang > 预留位(个人偏好/租户默认) > DEFAULT_LOCALE。"""
    headers = getattr(request, "headers", {}) or {}
    x_lang = normalize_locale(headers.get("X-Lang"))
    if x_lang:
        return x_lang
    # TODO(i18n-phase2): 个人偏好（JWT/用户配置） -> 租户默认
    return get_default_locale()


def get_current_locale() -> str:
    return LocaleContext.get() or get_default_locale()


@lru_cache(maxsize=None)
def _load_catalog(locale: str) -> dict:
    """懒加载 messages/<locale>.yaml；文件缺失返回空 dict（不报错）。

    数字键转 int，非数字键保留原样（为字符串键如 __validation_failed__ 留扩展空间）。
    """
    path = _MESSAGES_DIR / f"{locale}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    result = {}
    for k, v in data.items():
        try:
            result[int(k)] = v
        except (ValueError, TypeError):
            result[k] = v
    return result


def translate(code, locale=None, *, params=None, fallback=None) -> str:
    """按回落链取模板并渲染：

    1. locale 语言包
    2. DEFAULT_LOCALE 语言包
    3. fallback（异常的 default_message 或显式原文）
    4. str(code)

    命中模板后若传 params 则 str.format(**params)，缺参数吞异常返回未格式化模板。
    """
    locale = locale or get_current_locale()
    default_locale = get_default_locale()
    msg = _load_catalog(locale).get(code)
    if msg is None and locale != default_locale:
        msg = _load_catalog(default_locale).get(code)
    if msg is None:
        msg = fallback if fallback is not None else str(code)
    if params:
        try:
            return msg.format(**params)
        except (KeyError, IndexError):
            return msg
    return msg


def install_locale_middleware(app) -> None:
    """给 FastAPI 应用挂载 locale 中间件。

    请求进入时解析 X-Lang → LocaleContext.set()，结束时 reset。
    同时把 locale 写入 request.state.locale 便于路由透传。
    """

    @app.middleware("http")
    async def _locale_mw(request, call_next):
        token = LocaleContext.set(extract_locale(request))
        try:
            # 安全注入状态属性，供路由/透传使用
            request.state.locale = LocaleContext.get()
            return await call_next(request)
        finally:
            LocaleContext.reset(token)


def transmit_locale_header(headers: dict) -> None:
    """将当前上下文 locale 透传到下游转发 headers（网关/代理用）。

    调用方必须已挂载 install_locale_middleware，直接读 LocaleContext.get()。
    """
    locale = LocaleContext.get()
    if locale:
        headers["X-Lang"] = locale
