#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""对象存储抽象协议（ObjectStorage）。

D3/D5：凭证集中在业务侧配置，atomic 不直连 COS。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStorage(Protocol):
    """对象存储接口（异步优先）。"""

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        """上传字节到指定对象键，返回 key。"""
        ...

    async def get_bytes(self, key: str) -> bytes:
        """获取对象字节内容。"""
        ...

    async def get_to_path(self, key: str, dst_path: str) -> str:
        """将对象下载到本地临时文件，返回 dst_path。"""
        ...

    async def presigned_url(self, key: str, tenant_id: str, *, expires: int = 900) -> str:
        """生成预签名 GET URL（调用前需校验租户归属）。"""
        ...

    async def presigned_put_url(
        self, key: str, *, tenant_id: str, expires: int = 300, content_length: int | None = None
    ) -> str:
        """生成预签名 PUT URL（local 后端返回空字符串降级）。"""
        ...

    async def delete(self, key: str) -> bool:
        """删除指定对象。"""
        ...


__all__ = ["ObjectStorage"]
