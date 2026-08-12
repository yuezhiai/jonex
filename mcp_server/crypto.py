#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - MCP Key 哈希与校验（纯 stdlib）。

与主项目 jonex_core/common/crypto.py 算法一致：
  hash_mcp_key = hmac.new(JWT_SECRET, plaintext, sha256).hexdigest()
  verify_mcp_key = hmac.compare_digest(computed, stored_hash)

MCP Server 不生成 Key（不需要 generate_mcp_key），只校验。
纯 stdlib 实现，零 jonex_core 导入（避免 pydantic v1/v2 冲突）。
"""
import hashlib
import hmac
import os


def hash_mcp_key(plaintext: str) -> str:
    """对 MCP Key 明文做 HMAC-SHA256 哈希。

    从环境变量 JWT_SECRET 直接读取 HMAC key（不 import Settings 类，per D-02）。
    返回 64 字符 hex 字符串。

    Args:
        plaintext: MCP Key 明文（如 yxm_abc123...）

    Returns:
        64 字符 hex 哈希值

    Raises:
        RuntimeError: JWT_SECRET 未配置或为空（纵深防御——虽然 app.py 启动守卫会拦截，
                    但作为独立模块不应静默退化）
    """
    secret = os.getenv("JWT_SECRET", "").encode()
    if not secret:
        raise RuntimeError(
            "JWT_SECRET 环境变量未配置或为空，无法安全计算 MCP Key 哈希。"
            "请设置 JWT_SECRET 环境变量。"
        )
    return hmac.new(secret, plaintext.encode(), hashlib.sha256).hexdigest()


def verify_mcp_key(plaintext: str, stored_hash: str) -> bool:
    """校验 MCP Key 明文与存储的哈希是否匹配。

    使用 hmac.compare_digest() 常量时间比较（per D-24/SEC-04）。
    仅做哈希比对（单一职责），不做格式校验。
    空值输入返回 False。

    Args:
        plaintext: MCP Key 明文
        stored_hash: 存储的 hash 值（64 字符 hex）

    Returns:
        True 如果匹配，False 否则
    """
    if not plaintext or not stored_hash:
        return False
    return hmac.compare_digest(hash_mcp_key(plaintext), stored_hash)
