#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""对象存储工厂。

按 OBJECT_STORAGE_BACKEND 环境变量返回对应的存储后端：
- "cos" → CosObjectStorage
- "local" → LocalObjectStorage（默认，开发回退）
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from jonex_core.common import get_logger

logger = get_logger("object_storage")


def build_object_key(
    tenant_id: str,
    knowledge_base_id: str,
    doc_id: str,
    file_name: str | None,
) -> str:
    """统一的知识库文档对象存储 key 方案（local / cos / 其他对象存储通用）。

    形如：``{COS_KEY_PREFIX}/kb/{tenant}/{kb}/{doc}/{doc}_{safe_name}``

    - local 后端：物理文件落在 ``KB_INPUT_DIR/{key}``；
    - 对象存储后端（cos 等）：作为对象 Key 上传。

    key 与后端无关，是文档在平台对象存储中的规范标识。
    """
    prefix = os.getenv("COS_KEY_PREFIX", "jonex").strip("/")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", file_name or "unnamed")
    return f"{prefix}/kb/{tenant_id}/{knowledge_base_id}/{doc_id}/{doc_id}_{safe}"



# [jonex] §image-refs P1-1: 文档内嵌资产（图片等）扩展名白名单。
# aext 经 file_source 字符串旁路，理论上可被构造值注入——白名单化保证
# 它永不成为路径注入面（非白名单一律按 png 处理）。
_ASSET_EXT_WHITELIST = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def normalize_asset_ext(ext: str | None) -> str | None:
    """资产扩展名白名单化（image-reference-chain-execution-plan.md P1-1）。

    - 空值 → ``None``（调用方跳过上传/富化）；
    - 白名单外（含 ``../../etc/passwd`` 等路径穿越样本）→ 按 ``png``
      处理并打 WARNING。
    """
    if not ext:
        return None
    name = str(ext).strip().strip(".").lower()
    if name in _ASSET_EXT_WHITELIST:
        return name
    logger.warning("资产扩展名不在白名单，按 png 处理: %r", ext)
    return "png"


def build_asset_key(
    tenant_id: str,
    knowledge_base_id: str,
    doc_id: str,
    image_idx: int,
    ext: str | None,
) -> str:
    """图片等文档内嵌资产的对象键（image-reference-chain-execution-plan.md §4.2）。

    形如 ``{COS_KEY_PREFIX}/kb/{tenant}/{kb}/{doc}/assets/img_{idx}.{ext}``
    —— 与 build_object_key 的文档键同前缀同层级，便于按文档前缀批量清理。

    ext 经 normalize_asset_ext 白名单化；空值抛 ValueError（调用方应在
    有资产时再调用，见 P2-1「ext 缺失则跳过」的口径）。
    image_idx 强制为整数并抛 ValueError：raw 端点的入参来自请求 payload，
    不校验会允许 ``img_../../evil`` 之类路径穿越对象键。
    """
    safe_ext = normalize_asset_ext(ext)
    if not safe_ext:
        raise ValueError("asset ext 为空，调用前应先判定有值")
    try:
        idx = int(image_idx)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"image_idx 必须为整数: {image_idx!r}") from exc
    prefix = os.getenv("COS_KEY_PREFIX", "jonex").strip("/")
    return (
        f"{prefix}/kb/{tenant_id}/{knowledge_base_id}/{doc_id}"
        f"/assets/img_{idx}.{safe_ext}"
    )


@lru_cache(maxsize=1)
def get_object_storage():
    """获取对象存储实例（单例，lru_cache 保证进程内复用）。

    按环境变量 OBJECT_STORAGE_BACKEND 返回；用于**新上传**等以平台当前后端为准的场景。
    读取既有文档原文请改用 get_object_storage_for(doc.storage_backend)，按文档自身后端选择。

    启动时做一次连通性自检（仅 COS 后端），凭证错误尽早暴露。
    """
    backend = os.getenv("OBJECT_STORAGE_BACKEND", "local").strip().lower()

    if backend == "cos":
        from jonex_core.common.object_storage.cos_storage import CosObjectStorage

        instance = CosObjectStorage()
        instance.check_connectivity()  # 启动自检，凭证错误尽早暴露
        logger.info("对象存储后端: COS (腾讯云)")
    else:
        from jonex_core.common.object_storage.local_storage import LocalObjectStorage

        instance = LocalObjectStorage()
        logger.info("对象存储后端: local (开发回退)")

    return instance


@lru_cache(maxsize=4)
def get_object_storage_for(backend: str | None):
    """按**指定后端**返回对象存储实例（按 doc.storage_backend 选择，与全局 env 无关）。

    用于读取既有文档：混合数据（部分 local、部分 cos）时必须按每条文档自己的
    后端取对象，否则会出现「local 文档去 COS 读」之类的错配。
    """
    name = (backend or "local").strip().lower()
    if name == "cos":
        from jonex_core.common.object_storage.cos_storage import CosObjectStorage

        return CosObjectStorage()
    from jonex_core.common.object_storage.local_storage import LocalObjectStorage

    return LocalObjectStorage()


__all__ = [
    "build_asset_key",
    "build_object_key",
    "get_object_storage",
    "get_object_storage_for",
    "normalize_asset_ext",
]
