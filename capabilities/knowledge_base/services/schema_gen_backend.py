#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据源 schema 生成后端分派：进程内 local（默认）/ onto-router remote（回退）。

- `local`：调用 `jonex_core.capability.atomic.schema_gen` 的 vendored 生成器。
  纯规则、无网络/DB/LLM；同步 CPU，故放进 `asyncio.to_thread` 避免阻塞事件循环。
- `remote`：保留原行为，`POST {ONTO_INTEL_BASE}/schema-gen`。

开关 `SCHEMA_GEN_BACKEND` 缺省/空 = `local`。非法值是**服务端配置错误**
（`InternalError` 1000），响亮失败而不静默回落——照 `compile_backend.py` 的先例。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from jonex_core.common.exceptions import InternalError, InvalidParameterError
from jonex_core.common.i18n import translate

VALID_SCHEMA_GEN_BACKENDS = ("local", "remote")
DEFAULT_SCHEMA_GEN_BACKEND = "local"
_REMOTE_TIMEOUT_SECONDS = 300
_MAX_ERROR_CHARS = 300


class SchemaGenBackendConfigError(InternalError, ValueError):
    """`SCHEMA_GEN_BACKEND` 取值非法。

    **服务端配置**错误，不是调用方参数错误（照 `CompileBackendConfigError`）：
    同时继承 `ValueError`，保留运维/脚本 `except ValueError` 的自然写法；
    `InternalError.code` 是既有错误码，不自造。
    """


def resolve_backend() -> str:
    """读 `SCHEMA_GEN_BACKEND`；缺省/空 = local。"""
    value = (os.getenv("SCHEMA_GEN_BACKEND") or "").strip().lower()
    if not value:
        return DEFAULT_SCHEMA_GEN_BACKEND
    if value not in VALID_SCHEMA_GEN_BACKENDS:
        raise SchemaGenBackendConfigError(
            f"SCHEMA_GEN_BACKEND 取值非法：{value!r}"
            f"（可选 {VALID_SCHEMA_GEN_BACKENDS}）"
        )
    return value


def require_remote_base() -> str:
    """remote 模式必须有 `ONTO_INTEL_BASE`；缺失给可读的 1001。"""
    base = (os.getenv("ONTO_INTEL_BASE") or "").strip().rstrip("/")
    if not base:
        raise InvalidParameterError(
            message=translate(
                "err.datasource.schema_gen_not_configured",
                fallback="未配置 ONTO_INTEL_BASE，无法调用 schema 生成器",
            )
        )
    return base


def _generate_local_sync(
    catalog: dict[str, Any],
    domain_id: str,
    domain_name: str,
) -> dict[str, Any]:
    """同步入口（延迟 import：只在真正生成时拉入生成器与 zhconv）。"""
    from jonex_core.capability.atomic.schema_gen import generate_from_catalog

    return generate_from_catalog(catalog, domain_id, domain_name)


async def generate_local(
    catalog: dict[str, Any],
    domain_id: str,
    domain_name: str = "",
) -> dict[str, Any]:
    """进程内生成。生成器异常统一包装成 1001 `schema_gen_failed`（保留 `__cause__`）。"""
    try:
        return await asyncio.to_thread(
            _generate_local_sync,
            catalog,
            domain_id,
            domain_name,
        )
    except InvalidParameterError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 畸形 catalog 等都要变成可读 1001
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        raise InvalidParameterError(
            message=translate(
                "err.datasource.schema_gen_failed",
                params={"error": detail},
                fallback=f"schema 生成失败：{detail}",
            )
        ) from exc


async def generate_remote(
    base: str,
    catalog: dict[str, Any],
    domain_id: str,
    domain_name: str = "",
) -> dict[str, Any]:
    """回退到 onto-intelligent router；错误键与迁移前逐字一致。"""
    import httpx  # 延迟 import：local 模式不付出 httpx 依赖检查

    try:
        async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}/schema-gen",
                json={
                    "domain_id": domain_id,
                    "domain_name": domain_name,
                    "catalog": catalog,
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise InvalidParameterError(
            message=translate(
                "err.datasource.schema_gen_unreachable",
                params={"base": base},
                fallback=f"schema 生成器不可达（{base}）：{exc}",
            )
        ) from exc

    if resp.status_code != 200:
        raise InvalidParameterError(
            message=translate(
                "err.datasource.schema_gen_bad_status",
                params={"status": resp.status_code},
                fallback=f"schema 生成器返回 HTTP {resp.status_code}",
            )
        )

    try:
        result = resp.json()
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        raise InvalidParameterError(
            message=translate(
                "err.datasource.schema_gen_failed",
                params={"error": detail},
                fallback=f"schema 生成失败：{detail}",
            )
        ) from exc
    return result


__all__ = [
    "VALID_SCHEMA_GEN_BACKENDS",
    "DEFAULT_SCHEMA_GEN_BACKEND",
    "SchemaGenBackendConfigError",
    "resolve_backend",
    "require_remote_base",
    "generate_local",
    "generate_remote",
]
