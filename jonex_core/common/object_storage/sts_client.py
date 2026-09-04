#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""腾讯云 STS 临时密钥客户端（STS-01）。

用永久密钥（COS_SECRET_ID / COS_SECRET_KEY）换取按租户前缀收敛的临时密钥
（GetFederationToken），policy 收敛到 ``{COS_KEY_PREFIX}/kb/{tenant_id}/*``，
只授 ``PutObject`` + ``GetObject``，实现租户级最小权限 + 短时凭证。

依赖：tencentcloud-sdk-python-sts（官方 SDK，见 pyproject.toml）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from jonex_core.common import get_logger
from jonex_core.common.exceptions import ServiceUnavailableError

logger = get_logger("object_storage.sts")

# 预签名直传只写（PutObject）+ 预览下载（GetObject），不授删除/列表等越界动作。
_COS_ACTIONS = ["name/cos:PutObject", "name/cos:GetObject"]

# GetFederationToken 子账号最长 7200s（2h）。
_DEFAULT_DURATION_SECONDS = 7200


@dataclass(frozen=True)
class StsCredentials:
    """临时密钥三件套 + 绝对过期时间（UTC）。"""

    tmp_secret_id: str
    tmp_secret_key: str
    token: str
    expires_at: datetime


class StsClient:
    """STS 客户端：永久密钥 → 按租户收敛的临时密钥。

    同步接口（贴近官方 SDK）；异步包装由调用方（cos_storage / 缓存层）通过
    ``asyncio.to_thread`` 完成，与本模块解耦。
    """

    def __init__(self) -> None:
        self._secret_id = os.getenv("COS_SECRET_ID")
        self._secret_key = os.getenv("COS_SECRET_KEY")
        self._region = os.getenv("COS_REGION")
        self._bucket = os.getenv("COS_BUCKET")
        self._prefix = (os.getenv("COS_KEY_PREFIX") or "jonex").strip("/")

    def _require_config(self) -> None:
        """校验永久密钥换证所需配置，缺失时 fail-fast。"""
        required = {
            "COS_SECRET_ID": self._secret_id,
            "COS_SECRET_KEY": self._secret_key,
            "COS_REGION": self._region,
            "COS_BUCKET": self._bucket,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ServiceUnavailableError(
                message=f"STS 客户端初始化失败：缺少环境变量 {', '.join(missing)}。"
            )
        if not self._appid():
            raise ServiceUnavailableError(
                message="STS 客户端初始化失败：COS_BUCKET 必须形如 {name}-{appid}，无法提取 appid。"
            )

    def _appid(self) -> str:
        """从 bucket 名提取 appid（bucket 形如 ``{name}-{appid}``）。"""
        name, sep, appid = self._bucket.rpartition("-")
        return appid if sep and appid.isdigit() else ""

    def _build_policy(self, tenant_id: str) -> str:
        """构造按租户前缀收敛的 policy JSON 字符串。

        resource 形如 ``qcs::cos:{region}:uid/{appid}:{bucket}/{prefix}/kb/{tenant}/*``，
        把临时密钥能触碰的对象收敛到当前租户的知识库前缀下。
        """
        # 防御性校验：拒绝含通配符/路径分隔等危险字符的租户，避免污染 policy resource。
        if re.fullmatch(r"[A-Za-z0-9._-]+", tenant_id) is None:
            raise ServiceUnavailableError(message="tenant_id 含非法字符，拒绝构造 policy")
        resource = (
            f"qcs::cos:{self._region}:uid/{self._appid()}:"
            f"{self._bucket}/{self._prefix}/kb/{tenant_id}/*"
        )
        policy = {
            "version": "2.0",
            "statement": [
                {
                    "action": _COS_ACTIONS,
                    "effect": "allow",
                    "resource": [resource],
                }
            ],
        }
        return json.dumps(policy)

    def get_federation_token(
        self, tenant_id: str, duration_seconds: int = _DEFAULT_DURATION_SECONDS
    ) -> StsCredentials:
        """换取按租户收敛的临时密钥。

        Args:
            tenant_id: 业务租户，policy 收敛到该租户的知识库前缀。
            duration_seconds: 临时密钥有效期（默认 7200s = 2h）。

        Raises:
            ServiceUnavailableError: 缺配置或上游 STS 调用失败。
        """
        self._require_config()

        # 延迟 import：local 后端环境未装 tencentcloud SDK 时，本模块可被导入而不报错。
        from tencentcloud.common import credential
        from tencentcloud.sts.v20180813 import models, sts_client

        cred = credential.Credential(self._secret_id, self._secret_key)
        client = sts_client.StsClient(cred, self._region)

        req = models.GetFederationTokenRequest()
        req.Name = f"jonex-kb-{tenant_id}"
        req.Policy = self._build_policy(tenant_id)
        req.DurationSeconds = duration_seconds

        try:
            resp = client.GetFederationToken(req)
        except Exception as e:  # noqa: BLE001 - 统一转 JonexException，保留 cause
            raise ServiceUnavailableError(
                message="STS 临时密钥换取失败",
                details={"tenant_id": tenant_id},
                cause=e,
            ) from e

        creds = resp.Credentials
        expires_at = datetime.fromtimestamp(resp.ExpiredTime, tz=timezone.utc)
        return StsCredentials(
            tmp_secret_id=creds.TmpSecretId,
            tmp_secret_key=creds.TmpSecretKey,
            token=creds.Token,
            expires_at=expires_at,
        )


__all__ = ["StsClient", "StsCredentials"]
