#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""腾讯云 COS 对象存储实现（异步包装）。

依赖：cos-python-sdk-v5
凭证：子账号密钥（COS_SECRET_ID / COS_SECRET_KEY），遵循最小权限。
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from functools import lru_cache

from jonex_core.common import get_logger
from jonex_core.common.object_storage.sts_client import StsCredentials
from jonex_core.common.object_storage.sts_credentials_cache import StsCredentialsCache

logger = get_logger("object_storage.cos")


@lru_cache(maxsize=1)
def _client():
    """每个 region 单例复用 CosS3Client，避免连接/线程膨胀。

    from qcloud_cos import CosConfig, CosS3Client
    """
    from qcloud_cos import CosConfig, CosS3Client

    region = os.getenv("COS_REGION")
    secret_id = os.getenv("COS_SECRET_ID")
    secret_key = os.getenv("COS_SECRET_KEY")
    if not region or not secret_id or not secret_key:
        missing = [
            k
            for k, v in [("COS_REGION", region), ("COS_SECRET_ID", secret_id), ("COS_SECRET_KEY", secret_key)]
            if not v
        ]
        raise ValueError(
            f"COS 客户端初始化失败：缺少环境变量 {', '.join(missing)}。\n"
            "设置 OBJECT_STORAGE_BACKEND=cos 时必须配置 COS_REGION、COS_SECRET_ID、COS_SECRET_KEY。"
        )
    cfg = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=None,  # 永久密钥不传；临时密钥再注入
        Scheme="https",
        Timeout=300,  # 大文件上传/下载需要更长时间（默认 30s）
    )
    return CosS3Client(cfg)


def _make_temp_client(secret_id: str, secret_key: str, token: str):
    """构造带临时密钥 Token 的 CosS3Client（预签名用）。

    预签名是纯本地签名不涉及网络，但每次 new client 会重建线程池，故由调用方
    按租户缓存复用；token 变化即重建。
    """
    from qcloud_cos import CosConfig, CosS3Client

    region = os.getenv("COS_REGION")
    if not region:
        raise ValueError("COS 客户端初始化失败：缺少环境变量 COS_REGION。")
    cfg = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token,
        Scheme="https",
        Timeout=300,
    )
    return CosS3Client(cfg)


class CosObjectStorage:
    """腾讯云 COS 对象存储适配器。"""

    # 预签名 client 缓存容量上限：避免按租户无界增长（每租户一个带线程池的 client）。
    _PRESIGN_CLIENT_MAX = 128

    def __init__(self, sts_cache: StsCredentialsCache | None = None) -> None:
        bucket = os.getenv("COS_BUCKET")
        if not bucket:
            raise ValueError(
                "COS 存储初始化失败：缺少环境变量 COS_BUCKET。\n"
                "设置 OBJECT_STORAGE_BACKEND=cos 时必须配置 COS_BUCKET。"
            )
        self._bucket = bucket  # 形如 jonex-kb-1250000000
        self._expires = int(os.getenv("COS_PRESIGN_EXPIRES", "900"))
        self._sts_cache = sts_cache or StsCredentialsCache()
        # tenant_id -> (token, CosS3Client)：按租户缓存带临时密钥的签名 client。
        # OrderedDict 用于超限时按插入顺序淘汰最旧项（FIFO）。
        self._presign_clients: OrderedDict[str, tuple[str, object]] = OrderedDict()
        # tenant_id -> 锁：per-tenant 锁 + 双重检查，消除同租户并发 miss 的重复创建。
        self._presign_locks: dict[str, asyncio.Lock] = {}

    def check_connectivity(self) -> None:
        """自检 COS 凭证和 Bucket 连通性，失败抛 RuntimeError。

        用于工厂方法启动时 fail-fast，避免首次读写才暴露凭证错误。
        """
        try:
            _client().head_bucket(Bucket=self._bucket)
            logger.info("COS 连通性自检通过（Bucket: %s）", self._bucket)
        except Exception as e:
            raise RuntimeError(
                f"COS 连通性自检失败（Bucket: {self._bucket}）: {e}\n"
                "请检查 COS_REGION / COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET 环境变量配置。"
            ) from e

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        kw = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            kw["ContentType"] = content_type
        await asyncio.to_thread(_client().put_object, **kw)
        return key

    async def put_from_path(self, src_path: str, key: str, *, content_type: str | None = None) -> str:
        """上传本地文件到 COS。"""
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(
            _client().upload_file,
            Bucket=self._bucket, Key=key, LocalFilePath=src_path, **extra,
        )
        return key

    async def get_bytes(self, key: str) -> bytes:
        """获取对象字节内容。"""
        resp = await asyncio.to_thread(
            _client().get_object,
            Bucket=self._bucket, Key=key,
        )
        return resp["Body"].read()

    def fs_path(self, key: str) -> str | None:
        """COS 无本地文件路径；下游应改用 storage_key 通过 get_to_path 下载。"""
        return None

    async def get_to_path(self, key: str, dst_path: str) -> str:
        await asyncio.to_thread(
            _client().download_file,
            Bucket=self._bucket, Key=key, DestFilePath=dst_path,
        )
        return dst_path

    async def _presign_client(self, tenant_id: str):
        """按租户获取带临时密钥 Token 的签名 client（token 变化即重建）。

        命中缓存直接返回（热路径不做重操作）；miss 时用 per-tenant 锁 + 双重检查
        包裹「查缓存 → 必要时重建」，避免同租户并发 miss 时各建一个 client。
        缓存超限时按 FIFO 淘汰最旧租户，控制 client（内含线程池）数量。
        """
        creds = await self._sts_cache.get_credentials(tenant_id)
        cached = self._presign_clients.get(tenant_id)
        if cached is not None and cached[0] == creds.token:
            return cached[1], creds

        lock = self._presign_locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            # 双重检查：等待锁期间可能有别的协程已完成重建。
            cached = self._presign_clients.get(tenant_id)
            if cached is not None and cached[0] == creds.token:
                return cached[1], creds
            client = _make_temp_client(creds.tmp_secret_id, creds.tmp_secret_key, creds.token)
            self._presign_clients[tenant_id] = (creds.token, client)
            if len(self._presign_clients) > self._PRESIGN_CLIENT_MAX:
                evicted_tenant, _ = self._presign_clients.popitem(last=False)
                self._presign_locks.pop(evicted_tenant, None)
                # 被淘汰的 CosS3Client 无公开 shutdown/close 释放方法，删引用依赖 GC 回收其线程池；
                # 同步清掉被淘汰租户的锁，避免 _presign_locks 随租户无界增长。
            return client, creds

    async def presigned_url(self, key: str, tenant_id: str, *, expires: int | None = None,
                            disposition: str | None = None) -> str:
        """生成预签名 GET URL（纯本地签名，不涉及网络 I/O）。

        用按租户收敛的 STS 临时密钥签名；token 作为 x-cos-security-token 参数
        参与签名并出现在 URL 中（临时密钥预签名必须，否则 COS 拒绝）。
        disposition 缺省时强制 response-content-disposition=inline，保证 PDF/文本/
        图片等在浏览器/iframe 内联预览而非触发下载（content-type 仍取对象自身元数据）；
        下载场景传 "attachment; filename*=UTF-8''..." 触发浏览器下载并保持原文件名。
        """
        client, creds = await self._presign_client(tenant_id)
        return client.get_presigned_url(
            Method="GET",
            Bucket=self._bucket,
            Key=key,
            Expired=expires if expires is not None else self._expires,
            Params={
                "x-cos-security-token": creds.token,
                "response-content-disposition": disposition or "inline",
            },
        )

    async def presigned_put_url(
        self, key: str, *, tenant_id: str, expires: int = 300, content_length: int | None = None
    ) -> str:
        """生成预签名 PUT URL，用于前端直传 COS（D9）。

        content_length 非空时把 Content-Length 头纳入签名，锁死客户端必须上传
        该精确字节数（HTTP 层 + COS 签名层双重校验），用于限制直传文件大小。
        """
        client, creds = await self._presign_client(tenant_id)
        headers: dict = {}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        return client.get_presigned_url(
            Method="PUT",
            Bucket=self._bucket,
            Key=key,
            Expired=expires,
            Headers=headers,
            Params={"x-cos-security-token": creds.token},
        )

    async def head_object(self, key: str) -> bool:
        """检查对象是否存在（用于上传后确认）。"""
        try:
            await asyncio.to_thread(
                _client().head_object,
                Bucket=self._bucket, Key=key,
            )
            return True
        except Exception:
            return False

    async def head_object_size(self, key: str) -> int | None:
        """获取对象大小（Byte）；对象不存在或查询失败返回 None。

        用于 COS 直传模式的配额大小/容量校验：直传不经 Gateway，
        文件大小只能从对象存储元数据（Content-Length）读取。
        """
        try:
            resp = await asyncio.to_thread(
                _client().head_object,
                Bucket=self._bucket, Key=key,
            )
            content_length = resp.get("Content-Length")
            return int(content_length) if content_length is not None else None
        except Exception:
            return None

    async def delete(self, key: str) -> bool:
        await asyncio.to_thread(
            _client().delete_object,
            Bucket=self._bucket, Key=key,
        )
        return True


__all__ = ["CosObjectStorage"]
