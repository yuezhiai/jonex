#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""本地文件对象存储实现（开发回退，等价当前 /app/inputs 行为）。

D3/D9：local 后端下 presigned_url 返回空（前端需从 download 端点获取文件）。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from jonex_core.common import get_logger

logger = get_logger("object_storage.local")


class LocalObjectStorage:
    """本地文件系统存储适配器（开发回退）。"""

    def __init__(self) -> None:
        self._base_dir = Path(os.getenv("KB_INPUT_DIR", "/app/inputs"))
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # 兼容两种 storage_key 形态：
        #   1) 相对 key（如 "tenant/xxx.md"、"jonex/kb/..."）→ 拼到 base_dir 下
        #   2) 绝对路径（历史 file_path，如 "/app/inputs/tenant/xxx.md"）→ 直接使用
        # 都必须落在 base_dir 内，防止路径穿越。
        base = self._base_dir.resolve()
        raw = Path(key)
        if raw.is_absolute():
            full = raw.resolve()
        else:
            full = (self._base_dir / raw.as_posix().lstrip("/")).resolve()
        if full != base and not str(full).startswith(str(base) + os.sep):
            raise ValueError(f"路径穿越拒绝: {key}")
        return full

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        dst = self._resolve(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        logger.debug("local_storage: put %s (%d bytes)", key, len(data))
        return key

    async def get_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        return path.read_bytes()

    def fs_path(self, key: str) -> str:
        """返回 key 对应的本地绝对文件路径。

        供需要直接读取文件的下游使用（如 atomic-rag 解析直接读共享卷文件）。
        """
        return str(self._resolve(key))

    async def get_to_path(self, key: str, dst_path: str) -> str:
        src = self._resolve(key)
        shutil.copy2(str(src), dst_path)
        return dst_path

    async def presigned_url(self, key: str, tenant_id: str, *, expires: int = 900) -> str:
        """本地模式下无可公开访问 URL，返回空字符串（前端降级）。"""
        logger.debug("local_storage: presigned_url not supported (local backend)")
        return ""

    async def presigned_put_url(
        self, key: str, *, tenant_id: str, expires: int = 300, content_length: int | None = None
    ) -> str:
        """本地后端不支持预签名直传，返回空字符串（前端降级走 multipart 上传）。"""
        logger.debug("local_storage: presigned_put_url not supported (local backend)")
        return ""

    async def delete(self, key: str) -> bool:
        path = self._resolve(key)
        if path.exists():
            path.unlink()
            return True
        return False


__all__ = ["LocalObjectStorage"]
