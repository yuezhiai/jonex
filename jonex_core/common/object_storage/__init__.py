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
from datetime import datetime
from functools import lru_cache

from jonex_core.common import get_logger
from jonex_core.common.object_storage.sts_credentials_cache import StsCredentialsCache

logger = get_logger("object_storage")

# 模块级共享 STS 缓存单例：所有 Cos 存储实例共用同一份临时密钥缓存，
# 避免 get_object_storage 与 get_object_storage_for 各自 new 一份导致同租户重复换证。
_shared_sts_cache = StsCredentialsCache()


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


# [jonex] §image-refs B1：图片资产规范 MIME 映射。jpg 的规范 MIME 是 image/jpeg
#（非 image/jpg），浏览器不认非标准 MIME 会对预签名 URL 触发下载而非内联预览。
# 与 Reference/Rag-anything image_transport.py 的 _CONTENT_TYPE_MAP 保持一致。
_ASSET_EXT_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def asset_content_type(ext: str | None) -> str:
    """返回图片资产的规范 Content-Type（jpg → image/jpeg 等）。

    白名单外 / 空值统一按 application/octet-stream（调用方应先经
    normalize_asset_ext 白名单化；此函数只做 MIME 规范化，不再二次校验）。
    """
    name = normalize_asset_ext(ext)
    return _ASSET_EXT_MIME.get(name or "", "application/octet-stream")


#: `_ASSET_EXT_MIME` 的逆向表（MIME → 规范扩展名）。jpg/jpeg 撞同一个 MIME，
#: 靠 `setdefault` 取**先出现的那个**。
#: ⚠️ 必须遍历 `_ASSET_EXT_MIME`（dict，插入有序）而**不是** `_ASSET_EXT_WHITELIST`
#: （set，遍历顺序不确定）—— 用后者会让 image/jpeg 的扩展名在 jpg/jpeg 之间随机漂。
_ASSET_MIME_EXT: dict[str, str] = {}
for _ext, _mime in _ASSET_EXT_MIME.items():
    _ASSET_MIME_EXT.setdefault(_mime, _ext)


def asset_ext_for_mime(mime: str | None) -> str | None:
    """MIME → 规范扩展名（`asset_content_type` 的逆向）；不认识返回 ``None``。

    用于「先有 MIME、要反推对象键扩展名」的场景（如以图问知识的查询图片：
    MIME 由 magic bytes 判定，扩展名只是为了键好看与后端 Content-Type 推断）。
    """
    base = (mime or "").split(";", 1)[0].strip().lower()
    return _ASSET_MIME_EXT.get(base)


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


def build_preview_key(
    tenant_id: str,
    knowledge_base_id: str,
    doc_id: str,
) -> str:
    """office 文档的预览 PDF 对象键（office-查看原文定位方案 A）。

    形如 ``{COS_KEY_PREFIX}/kb/{tenant}/{kb}/{doc}/preview.pdf``
    —— 与 build_object_key / build_asset_key 同前缀同层级，便于按文档前缀批量清理。
    仅 office 文档在解析侧上传（OfficePreviewUploadStage），查看原文时据此取预签名 URL。
    """
    prefix = os.getenv("COS_KEY_PREFIX", "jonex").strip("/")
    return f"{prefix}/kb/{tenant_id}/{knowledge_base_id}/{doc_id}/preview.pdf"


def build_parse_md_key(tenant_id: str, knowledge_base_id: str, doc_id: str) -> str:
    """解析产物 markdown 的对象键（解析质量报告的证据载体）。

    形如 ``{COS_KEY_PREFIX}/kb/{tenant}/{kb}/{doc}/parse.md``
    —— 与 build_preview_key 同前缀同层级（无 ext 参数、固定文件名），便于按文档前缀批量清理。
    """
    prefix = os.getenv("COS_KEY_PREFIX", "jonex").strip("/")
    return f"{prefix}/kb/{tenant_id}/{knowledge_base_id}/{doc_id}/parse.md"


def build_query_image_key(
    tenant_id: str,
    image_id: str,
    ext: str | None,
    created_at: "datetime | None" = None,
) -> str:
    """以图问知识的查询图片对象键（执行计划 §4-B10）。

    形如 ``{COS_KEY_PREFIX}/kb/{tenant}/query-images/{yyyymm}/{image_id}.{ext}``。

    ⚠️ **必须落在 ``{prefix}/kb/{tenant}/`` 之下**：COS 预签名用的 STS policy 只授权
    ``{prefix}/kb/{tenant_id}/*``（`sts_client.py`），放别处签出来的 URL 一律 403。
    ``query-images`` 这个段名不会与 KB 段冲突 —— KB id 是 32 位 hex。

    按月分目录（``yyyymm``）纯为便于运维按前缀排查与批量清理，不参与任何业务判定。

    ext 经 `normalize_asset_ext` 白名单化（非白名单按 png 并打 WARNING），
    因此它永远不构成路径注入面；空值抛 ValueError。
    """
    safe_ext = normalize_asset_ext(ext)
    if not safe_ext:
        raise ValueError("查询图片 ext 为空，调用前应先判定有值")
    month = (created_at or datetime.now()).strftime("%Y%m")
    prefix = os.getenv("COS_KEY_PREFIX", "jonex").strip("/")
    return f"{prefix}/kb/{tenant_id}/query-images/{month}/{image_id}.{safe_ext}"


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

        instance = CosObjectStorage(sts_cache=_shared_sts_cache)
        instance.check_connectivity()  # 启动自检，凭证错误尽早暴露
        logger.info("对象存储后端: COS (腾讯云)")
    elif backend == "s3":
        from jonex_core.common.object_storage.s3_storage import S3ObjectStorage

        instance = S3ObjectStorage()
        instance.check_connectivity()  # 启动自检，凭证错误尽早暴露
        logger.info("对象存储后端: S3 (兼容)")
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

        return CosObjectStorage(sts_cache=_shared_sts_cache)
    if name == "s3":
        from jonex_core.common.object_storage.s3_storage import S3ObjectStorage

        return S3ObjectStorage()
    from jonex_core.common.object_storage.local_storage import LocalObjectStorage

    return LocalObjectStorage()


__all__ = [
    "asset_content_type",
    "asset_ext_for_mime",
    "build_asset_key",
    "build_object_key",
    "build_parse_md_key",
    "build_preview_key",
    "build_query_image_key",
    "get_object_storage",
    "get_object_storage_for",
    "normalize_asset_ext",
]
