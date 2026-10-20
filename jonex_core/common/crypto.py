#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""对称加密（凭据可逆存储）+ ingest key 哈希。

- 凭据：Fernet 对称加密，密钥来自 DATA_SOURCE_SECRET_KEY（base64 urlsafe 32B）。
- ingest key / MCP Key：HMAC-SHA256（MCP_KEY_SECRET 作 key，未配置时回退 JWT_SECRET），
  单向，仅存哈希。

  ⚠️ 两类 key 的 legacy 策略**不同**，不可混用同一套 verify：
  - **MCP Key**（yxm_*）：明文是 token_urlsafe(32) 高熵随机串 ⇒ 「已知盐 + 未知明文」
    不可行，故 verify 额外尝试 MCP_KEY_SECRET_LEGACY，使盐可轮换而无需重发。
  - **ingest key**（yxk_*）：payload 是 base64(tenant|kb|ds|rand) **明文可读** ⇒ 是
    「已知盐 + 已知明文」，攻击者可自造 payload 并签出合法签名；且 decode_ingest_key
    只解析不验签、租户由 payload 决定。故 ingest key **只认当前盐、不做 legacy 兜底**，
    盐轮换时必须重新下发。
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
    # 只认当前盐，**不做 legacy 兜底**（与 verify_mcp_key 的关键差异）：
    # ingest key 的 payload 是 base64 明文，盐一旦公开即可自造 payload 伪造签名，
    # 而 decode_ingest_key 只解析不验签、租户由 payload 决定 ⇒ 兜底等于开放越权写入。
    # 代价是盐轮换后存量 ingest key 必须重新下发（数量少，可接受）。
    return hmac.compare_digest(_hmac_hex(_current_hash_secret(), plaintext), stored_hash)


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


def generate_write_key() -> str:
    """生成知识写入 Key（yxm_ 前缀 + 32 字节 URL-safe 随机串）。

    格式: yxm_{token_urlsafe(32)}，总长度约 47 字符。
    运行时真实前缀统一 yxm_（per PRD §8.4），mcpw_ 仅为 UI 脱敏展示前缀。
    调用方自行提取 key_prefix 存入 DB，本函数不负责。
    """
    return f"yxm_{secrets.token_urlsafe(32)}"


def hash_mcp_key(plaintext: str) -> str:
    """对 MCP Key 明文做 HMAC-SHA256 哈希（单向，仅存哈希）。

    复用 _sign_payload() 实现，与 hash_ingest_key() 同算法。
    前缀无关（yxm_ / mcpw_ 均适用）。
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
    return _verify_payload(plaintext, stored_hash)


# ── MCP Key / ingest key 的 HMAC 盐（与 JWT 签名根解耦） ──
# 这两类 key 的存储值都是 HMAC-SHA256(盐, 明文)，盐原为 JWT_SECRET ⇒ 一旦轮换 JWT_SECRET，
# 存量 key 的哈希全部失配、必须重发。为此把盐独立出来：
#   MCP_KEY_SECRET        —— 当前盐；未配置时回退 JWT_SECRET（兼容既有部署与测试）
#   MCP_KEY_SECRET_LEGACY —— 旧盐，仅在 verify 时兜底；存量 key 无需重发
# 保留旧盐不构成伪造风险：这些 key 的明文是高熵随机串（token_urlsafe(32)），
# 攻击面是「已知盐 + 未知明文」的预映像攻击，不可行——与 JWT 签名的
# 「已知盐 + 已知明文」可任意伪造不同。
MCP_KEY_SECRET_ENV = "MCP_KEY_SECRET"
MCP_KEY_SECRET_LEGACY_ENV = "MCP_KEY_SECRET_LEGACY"


def _current_hash_secret() -> bytes:
    """当前 HMAC 盐：优先 MCP_KEY_SECRET，未配置回退 JWT_SECRET（兼容旧部署）。"""
    secret = os.getenv(MCP_KEY_SECRET_ENV, "").strip() or os.getenv("JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            f"{MCP_KEY_SECRET_ENV} 与 JWT_SECRET 环境变量均未配置或为空，无法安全计算哈希。"
            f"请设置 {MCP_KEY_SECRET_ENV} 环境变量（推荐）或 JWT_SECRET。"
        )
    return secret.encode()


def _legacy_hash_secret() -> bytes | None:
    """旧盐，仅用于 verify 兜底；未配置返回 None。"""
    secret = os.getenv(MCP_KEY_SECRET_LEGACY_ENV, "").strip()
    return secret.encode() if secret else None


def _hmac_hex(secret: bytes, data: str) -> str:
    return hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()


def _sign_payload(data: str) -> str:
    """HMAC-SHA256 签名（与 mcp_server/crypto.py 算法一致，修改前需同步）。"""
    return _hmac_hex(_current_hash_secret(), data)


def _verify_payload(data: str, stored_hash: str) -> bool:
    """校验签名：先试当前盐，未命中再试 legacy 盐（盐轮换的平滑过渡）。

    ⚠️ **仅可用于 MCP Key**（明文高熵不可猜）。ingest key 必须走单盐比对，
    见 verify_ingest_key 的说明——否则 legacy 盐一旦是公开值即可被伪造。
    """
    if not stored_hash:
        return False
    if hmac.compare_digest(_hmac_hex(_current_hash_secret(), data), stored_hash):
        return True
    legacy = _legacy_hash_secret()
    return legacy is not None and hmac.compare_digest(_hmac_hex(legacy, data), stored_hash)


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


# ── 查询图片（以图问知识）查看短时 token ──
# 与上面那个原文查看 token 两处关键差异，都是有意为之：
#   ① **claims 带 user** —— 查询图片按 tenant + user 隔离（D10），只绑租户就等于
#      同租户任何人拿到链接都能看图；
#   ② **raw 端点只认 token、不回退 `X-Tenant-ID` 头** —— 原文查看那条回退是历史包袱，
#      查询图片没有它，少了「无票也能读」的路径。
_QUERY_IMAGE_TOKEN_SCOPE = "kb_query_image_view"


def generate_query_image_token(
    tenant_id: str, user_id: str, image_id: str, ttl: int = 900
) -> str:
    """签发只读、单图、短时效的查询图片查看 token。"""
    import time

    import jwt

    payload = {
        "scope": _QUERY_IMAGE_TOKEN_SCOPE,
        "tid": tenant_id,
        "uid": user_id,
        "img": image_id,
        "exp": int(time.time()) + int(ttl),
    }
    cfg = get_config()
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=cfg.JWT_ALGORITHM)


def verify_query_image_token(token: str) -> dict | None:
    """校验查询图片查看 token，返回 ``{"tenant_id", "user_id", "image_id"}``；无效返回 None。

    scope 对不上（例如拿原文查看 token 来换）也算无效。
    """
    if not token:
        return None
    import jwt

    cfg = get_config()
    try:
        payload = jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("scope") != _QUERY_IMAGE_TOKEN_SCOPE:
        return None
    tid = payload.get("tid")
    uid = payload.get("uid")
    img = payload.get("img")
    if not tid or not uid or not img:
        return None
    return {"tenant_id": tid, "user_id": uid, "image_id": img}


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
    "generate_query_image_token",
    "verify_query_image_token",
    "generate_mcp_key",
    "hash_mcp_key",
    "verify_mcp_key",
    "generate_write_key",
]
