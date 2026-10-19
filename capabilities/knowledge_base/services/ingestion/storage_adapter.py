#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""文件存储直连适配器（外部 S3 / MinIO，一期）。

连接的是客户外部桶（与平台 object_storage 无关）。用 boto3（S3 兼容，
MinIO 通过 endpoint_url 指向 MinIO 服务）。

安全（渗透报告 3.1 / RC-C，SSRF）：``endpoint_url`` 是**用户可控出站目标**，与 api
适配器**同档** —— 出站前过 ``validate_kb_ingestion_url``（严格档：阻断私网 / 回环 /
链路本地 / 云元数据；``KB_INGESTION_ALLOWED_HOSTS`` 白名单是逃生阀）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from jonex_core.common.config import get_config
from jonex_core.common.crypto import decrypt_secret
from jonex_core.common.i18n import translate
from jonex_core.security.outbound_url_validator import validate_kb_ingestion_url

from .base import RemoteFile

logger = logging.getLogger(__name__)


class StorageIngestionAdapter:
    def _client(self, cfg: dict[str, Any]):
        import boto3
        from botocore.config import Config

        cred = decrypt_secret(cfg.get("credential_ref", ""))  # "ak:sk"
        ak, _, sk = cred.partition(":")
        backend = (cfg.get("backend") or "").strip().lower()
        bucket = (cfg.get("bucket") or "").strip()
        endpoint = cfg.get("endpoint") or None

        # 寻址风格：MinIO 必须 path-style；COS / AWS S3 必须 virtual-hosted。
        style = "path" if backend == "minio" else "virtual"

        # COS/AWS（virtual 寻址）下，若用户把 endpoint 填成了「bucket 子域」式
        # （https://<bucket>.cos.<region>.myqcloud.com），boto3 还会再拼一次 bucket，
        # 造成 <bucket>.<bucket>... 重复。这里归一化：剥掉 bucket 子域得到区域端点，
        # 让用户填「区域端点」或「bucket 子域端点」两种都可用。
        if style == "virtual" and endpoint and bucket:
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(endpoint)
            if parts.netloc.startswith(f"{bucket}."):
                endpoint = urlunsplit(
                    (parts.scheme, parts.netloc[len(bucket) + 1:], "", "", "")
                )

        if endpoint:
            # 用户可控出站目标（endpoint_url）→ 与 api 适配器同档的严格 SSRF 校验。
            validate_kb_ingestion_url(
                endpoint, allowed_hosts=get_config().kb_ingestion_allowed_hosts
            )

        return boto3.client(
            "s3",
            endpoint_url=endpoint,  # MinIO: http(s)://host:9000；COS: https://cos.<region>.myqcloud.com 或 https://<bucket>.cos.<region>.myqcloud.com
            aws_access_key_id=ak or None,
            aws_secret_access_key=sk or None,
            region_name=cfg.get("region") or None,
            config=Config(s3={"addressing_style": style}),
        )

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            files = await self.list_remote_files(cfg)
            return {"ok": True, "message": translate("success.connection", fallback="连接成功"), "sample_count": len(files)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    async def list_remote_files(
        self, cfg: dict[str, Any], since: Optional[datetime] = None,
    ) -> list[RemoteFile]:
        def _list() -> list[RemoteFile]:
            client = self._client(cfg)
            bucket = cfg["bucket"]
            prefix = cfg.get("prefix", "")
            allow = {e.lower().lstrip(".") for e in (cfg.get("include_ext") or [])}
            out: list[RemoteFile] = []
            token: Optional[str] = None
            while True:
                kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kw["ContinuationToken"] = token
                resp = client.list_objects_v2(**kw)
                for o in resp.get("Contents", []):
                    key = o["Key"]
                    if key.endswith("/"):
                        continue
                    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
                    if allow and ext not in allow:
                        continue
                    lm = o.get("LastModified")
                    if since and lm and lm <= since:
                        continue
                    out.append(
                        RemoteFile(
                            external_id=key,
                            name=key.rsplit("/", 1)[-1],
                            size=o.get("Size"),
                            modified_at=lm,
                            uri=key,
                        )
                    )
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
            return out

        return await asyncio.to_thread(_list)

    async def fetch_bytes(self, cfg: dict[str, Any], rf: RemoteFile) -> tuple[bytes, str]:
        def _get() -> tuple[bytes, str]:
            client = self._client(cfg)
            resp = client.get_object(Bucket=cfg["bucket"], Key=rf.uri)
            return resp["Body"].read(), resp.get("ContentType", "application/octet-stream")

        return await asyncio.to_thread(_get)


__all__ = ["StorageIngestionAdapter"]
