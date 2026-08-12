#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""对称加密（凭据可逆存储）+ ingest key 哈希。

- 凭据：Fernet 对称加密，密钥来自 DATA_SOURCE_SECRET_KEY（base64 urlsafe 32B）。
- ingest key：HMAC-SHA256（JWT_SECRET 作 key），单向，仅存哈希。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from functools import lru_cache

from jonex_core.common import get_config, get_logger

logger = get_logger("crypto")


@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet

    key = os.getenv("DATA_SOURCE_SECRET_KEY", "").strip()
    if not key:
        # 开发回退：从 JWT_SECRET 派生（生产必须显式配置 DATA_SOURCE_SECRET_KEY）
        secret = get_config().JWT_SECRET.encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest()).decode()
        logger.warning("DATA_SOURCE_SECRET_KEY 未配置，已从 JWT_SECRET 派生（生产请显式配置）")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()


def generate_ingest_key(tenant_id: str = "", kb_id: str = "", ds_id: str = "") -> str:
    """生成 ingest key（自包含 tenant_id + kb_id + ds_id，HMAC 防篡改）。

    格式: yxk_{base64(tenant|kb|ds|random)}.{signature_hex}
    """
    payload = f"{tenant_id}|{kb_id}|{ds_id}|{secrets.token_hex(16)}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = _sign_payload(encoded)
    return f"yxk_{encoded}.{sig}"


def hash_ingest_key(plaintext: str) -> str:
    """仅保留 HMAC 签名部分用于存储校验（不存明文 payload）。"""
    return _sign_payload(plaintext)


def verify_ingest_key(plaintext: str, stored_hash: str) -> bool:
    if not plaintext or not stored_hash:
        return False
    # 对整串 key 做 HMAC，和存储的签名比对
    return hmac.compare_digest(_sign_payload(plaintext), stored_hash)


def decode_ingest_key(plaintext: str) -> dict | None:
    """解析 ingest key 中的 tenant_id / kb_id / ds_id。解析失败返回 None。"""
    try:
        # 去掉 yxk_ 前缀
        body = plaintext
        if body.startswith("yxk_"):
            body = body[4:]
        # 分离 payload 和 signature
        if "." in body:
            body = body.rsplit(".", 1)[0]
        # base64 decode（补回 padding）
        padded = body + "=" * (4 - len(body) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        parts = payload.split("|")
        if len(parts) >= 3:
            return {"tenant_id": parts[0], "kb_id": parts[1], "ds_id": parts[2]}
    except Exception:
        pass
    return None


def generate_mcp_key() -> str:
    """生成 MCP Key（yxm_ 前缀 + 32 字节 URL-safe 随机串）。

    格式: yxm_{token_urlsafe(32)}，总长度约 47 字符。
    调用方自行提取 key_prefix（前 8 位）存入 DB，本函数不负责。
    """
    return f"yxm_{secrets.token_urlsafe(32)}"


def hash_mcp_key(plaintext: str) -> str:
    """对 MCP Key 明文做 HMAC-SHA256 哈希（单向，仅存哈希）。

    复用 _sign_payload() 实现，与 hash_ingest_key() 同算法。
    """
    return _sign_payload(plaintext)


def verify_mcp_key(plaintext: str, stored_hash: str) -> bool:
    """校验 MCP Key 明文与存储的哈希是否匹配。

    仅做哈希比对（单一职责），不做格式校验（yxm_ 前缀/长度/字符集）。
    格式校验由调用方在 hash 前独立完成（per D-05）。
    使用 hmac.compare_digest() 常量时间比较（per D-24/SEC-04）。
    """
    if not plaintext or not stored_hash:
        return False
    return hmac.compare_digest(hash_mcp_key(plaintext), stored_hash)


def _sign_payload(data: str) -> str:
    """HMAC-SHA256 签名（与 mcp_server/crypto.py 算法一致，修改前需同步）。"""
    secret = os.getenv("JWT_SECRET", "").encode()
    if not secret:
        raise RuntimeError(
            "JWT_SECRET 环境变量未配置或为空，无法安全计算哈希。"
            "请设置 JWT_SECRET 环境变量。"
        )
    return hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()


# ── 文档原文查看短时 token（音视频/PDF/图片直连播放用） ──
# 复用 JWT_SECRET 签名，scope claim 区分用途；绑定单个 doc_id + 租户 + 短 TTL。
_VIEW_TOKEN_SCOPE = "kb_raw_view"


def generate_view_token(tenant_id: str, doc_id: str, ttl: int = 300) -> str:
    """签发只读、单文档、短时效的原文查看 token。

    用于 ``<video>/<audio>`` 等无法携带 Authorization 头的直连场景。
    """
    import time

    import jwt

    payload = {
        "scope": _VIEW_TOKEN_SCOPE,
        "tid": tenant_id,
        "doc": doc_id,
        "exp": int(time.time()) + int(ttl),
    }
    cfg = get_config()
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def verify_view_token(token: str) -> dict | None:
    """校验查看 token，返回 ``{"tenant_id", "doc_id"}``，无效返回 None。"""
    if not token:
        return None
    import jwt

    cfg = get_config()
    try:
        payload = jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("scope") != _VIEW_TOKEN_SCOPE:
        return None
    tid = payload.get("tid")
    doc = payload.get("doc")
    if not tid or not doc:
        return None
    return {"tenant_id": tid, "doc_id": doc}


__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "generate_ingest_key",
    "hash_ingest_key",
    "verify_ingest_key",
    "decode_ingest_key",
    "generate_view_token",
    "verify_view_token",
    "generate_mcp_key",
    "hash_mcp_key",
    "verify_mcp_key",
]
