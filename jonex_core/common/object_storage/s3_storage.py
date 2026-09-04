#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""S3 兼容对象存储实现（AWS S3 / MinIO / 国内云 S3 兼容端点）。

依赖：boto3（已在项目依赖中）。静态 AK/SK 认证，与 COS 永久密钥的配置方式对齐：
- 必填：S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_BUCKET
- S3_REGION：直连 AWS S3（未设 S3_ENDPOINT_URL）时必填（它决定 endpoint 主机名）；
        自建端点（MinIO 等无多区域概念）可省略，默认 us-east-1 —— SigV4 凭证范围
        要求 region 非空，但 MinIO 缺省站点 region 正是 us-east-1
- 可选：S3_ENDPOINT_URL（MinIO / S3 兼容端点；AWS S3 可不填）
        S3_ADDRESSING_STYLE（auto | path | virtual，默认 auto = 自动推导：
            有 S3_ENDPOINT_URL 取 path，否则取 virtual。不会把 auto 下传给 botocore，
            原因见 _make_client 内注释——botocore 的 auto 会丢掉 region 导致 403）
        S3_SESSION_TOKEN（临时凭证场景，暂未使用）
        S3_PRESIGN_EXPIRES（预签名有效期秒，默认 900）
"""

from __future__ import annotations

import asyncio
import os

from jonex_core.common import get_logger

logger = get_logger("object_storage.s3")


def _make_client():
    import boto3
    from botocore.config import Config

    endpoint = os.getenv("S3_ENDPOINT_URL") or None
    region = os.getenv("S3_REGION")
    access_key = os.getenv("S3_ACCESS_KEY_ID")
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY")
    bucket = os.getenv("S3_BUCKET")

    # [jonex] MinIO 等自建端点没有多区域概念：站点 region 缺省即 us-east-1，只有显式配了
    # MINIO_SITE_REGION 才需要对齐（不匹配会回 AuthorizationHeaderMalformed）。但 SigV4 的
    # 凭证范围 (credential scope) 必须含 region 字段，boto3 侧不能留空，故这里兜底，
    # 免得 MinIO 部署方还得自己猜要填什么。
    # AWS S3（无 endpoint）不做兜底：region 直接决定 endpoint 主机名，猜错会签出错误主机名
    # 而签名带另一个 region，一律 403，必须由部署显式声明。
    if not region and endpoint:
        region = "us-east-1"
        logger.info("S3_REGION 未设置，自建端点场景默认 us-east-1（endpoint=%s）", endpoint)

    required = [
        ("S3_ACCESS_KEY_ID", access_key),
        ("S3_SECRET_ACCESS_KEY", secret_key),
        ("S3_BUCKET", bucket),
    ]
    if not endpoint:
        required.insert(0, ("S3_REGION", region))
    missing = [k for k, v in required if not v]
    if missing:
        raise ValueError(
            f"S3 客户端初始化失败：缺少环境变量 {', '.join(missing)}。\n"
            "设置 OBJECT_STORAGE_BACKEND=s3 时必须配置 "
            "S3_ACCESS_KEY_ID、S3_SECRET_ACCESS_KEY、S3_BUCKET；"
            "S3_REGION 在直连 AWS S3（未设 S3_ENDPOINT_URL）时也必填，"
            "自建端点（MinIO 等）可省略，默认 us-east-1。"
        )
    # [jonex] 绝不把 addressing_style="auto" 下传给 botocore：无 endpoint_url 时它会生成
    # 传统全局虚拟主机名 <bucket>.s3.amazonaws.com（region 段被抹掉），而签名用的是真实
    # region，AWS 侧 SigV4 校验失败，预览/下载全部 403。实测 botocore 1.43.81：
    #   auto    -> bucket.s3.amazonaws.com                    错，403
    #   virtual -> bucket.s3.<region>.amazonaws.com            对
    #   path    -> s3.<region>.amazonaws.com/<bucket>/...      对
    # 故 auto/缺省/非法值一律在这里推导，只把 path|virtual 交给 botocore：
    #   有 endpoint_url（MinIO 等自建端点，通常没有泛域名 DNS）→ path
    #   无 endpoint_url（AWS S3）→ virtual（AWS 推荐形式，path-style 已被 AWS 标记弃用）
    style = (os.getenv("S3_ADDRESSING_STYLE") or "").strip().lower()
    if style not in ("path", "virtual"):
        if style not in ("", "auto"):
            logger.warning(
                "S3_ADDRESSING_STYLE=%s 非法（有效值 auto/path/virtual），按自动推导处理", style
            )
        style = "path" if endpoint else "virtual"
        logger.info(
            "S3 addressing_style 自动推导为 %s（endpoint_url=%s）", style, endpoint or "<AWS 默认>"
        )
    session_token = os.getenv("S3_SESSION_TOKEN") or None

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name=region,
        config=Config(
            s3={"addressing_style": style},
            signature_version="s3v4",
        ),
    )


class S3ObjectStorage:
    """S3 兼容对象存储适配器（静态 AK/SK）。"""

    def __init__(self) -> None:
        self._bucket = os.getenv("S3_BUCKET")
        if not self._bucket:
            raise ValueError(
                "S3 存储初始化失败：缺少环境变量 S3_BUCKET。\n"
                "设置 OBJECT_STORAGE_BACKEND=s3 时必须配置 S3_BUCKET。"
            )
        self._expires = int(os.getenv("S3_PRESIGN_EXPIRES", "900"))
        self._client = _make_client()

    def check_connectivity(self) -> None:
        """自检 S3 凭证和 Bucket 连通性，失败抛 RuntimeError。"""
        try:
            self._client.head_bucket(Bucket=self._bucket)
            logger.info("S3 连通性自检通过（Bucket: %s）", self._bucket)
        except Exception as e:
            raise RuntimeError(
                f"S3 连通性自检失败（Bucket: {self._bucket}）: {e}\n"
                "请检查 S3_REGION / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / "
                "S3_BUCKET / S3_ENDPOINT_URL / S3_ADDRESSING_STYLE 环境变量配置。"
            ) from e

    async def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        kw = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            kw["ContentType"] = content_type
        await asyncio.to_thread(self._client.put_object, **kw)
        return key

    async def put_from_path(self, src_path: str, key: str, *, content_type: str | None = None) -> str:
        """上传本地文件到 S3。"""
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(
            self._client.upload_file,
            Filename=src_path,
            Bucket=self._bucket,
            Key=key,
            ExtraArgs=extra,
        )
        return key

    async def get_bytes(self, key: str) -> bytes:
        resp = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket, Key=key,
        )
        return resp["Body"].read()

    def fs_path(self, key: str) -> str | None:
        """S3 无本地文件路径；下游应改用 storage_key 通过 get_to_path 下载。"""
        return None

    async def get_to_path(self, key: str, dst_path: str) -> str:
        await asyncio.to_thread(
            self._client.download_file,
            Bucket=self._bucket, Key=key, Filename=dst_path,
        )
        return dst_path

    async def presigned_url(self, key: str, tenant_id: str, *, expires: int | None = None,
                            disposition: str | None = None) -> str:
        """生成预签名 GET URL（静态 AK/SK 本地签名，不涉及网络 I/O）。

        disposition 缺省时强制 ResponseContentDisposition=inline，与 COS 后端行为一致
        （PDF/文本/图片在浏览器内联预览）；下载场景传 attachment; filename*=... 触发下载。
        tenant_id 在静态密钥模式下仅保留协议一致性，当前不参与签名。
        """
        params = {
            "Bucket": self._bucket,
            "Key": key,
            "ResponseContentDisposition": disposition or "inline",
        }
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params=params,
            ExpiresIn=expires if expires is not None else self._expires,
        )

    async def presigned_put_url(
        self, key: str, *, tenant_id: str, expires: int = 300, content_length: int | None = None
    ) -> str:
        """生成预签名 PUT URL，用于前端直传 S3。

        content_length 非空时纳入 SigV4 签名（`ContentLength` 进 Params），锁死客户端
        必须上传该精确字节数——与 COS 直传的长度锁行为一致。不加锁的话一个签名 URL
        可上传任意大小对象，绕过 generate_upload_url 的 _MAX_UPLOAD_SIZE 校验：
        攻击者只上传不 confirm，超大对象已落 bucket 且无清理机制（纯存储成本消耗）。

        tenant_id 在静态密钥模式下仅保留协议一致性，不参与签名。
        """
        params: dict = {"Bucket": self._bucket, "Key": key}
        if content_length is not None:
            params["ContentLength"] = int(content_length)
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "put_object",
            Params=params,
            ExpiresIn=expires,
        )

    async def head_object(self, key: str) -> bool:
        """检查对象是否存在。"""
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket, Key=key,
            )
            return True
        except Exception:
            return False

    async def head_object_size(self, key: str) -> int | None:
        """获取对象大小（Byte）；对象不存在或查询失败返回 None。"""
        try:
            resp = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket, Key=key,
            )
            length = resp.get("ContentLength")  # S3 字段无连字符（COS 是 Content-Length）
            return int(length) if length is not None else None
        except Exception:
            return None

    async def delete(self, key: str) -> bool:
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket, Key=key,
        )
        return True


__all__ = ["S3ObjectStorage"]
