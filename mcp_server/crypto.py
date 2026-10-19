#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP Server - MCP Key 哈希与校验（纯 stdlib）。

与主项目 jonex_core/common/crypto.py 算法一致：
  hash_mcp_key     = hmac.new(当前盐, plaintext, sha256).hexdigest()
  verify_mcp_key   = 当前盐命中，或 MCP_KEY_SECRET_LEGACY 兜底命中
  candidate_hashes = 当前盐 + LEGACY 的全部候选哈希（供「按 hash 查表」的鉴权侧使用）

盐解析规则（两端必须同批修改，tests/unit/test_crypto.py 的跨进程一致性测试会兜住漂移）：
  MCP_KEY_SECRET        当前盐；未配置时回退 JWT_SECRET（兼容既有部署）
  MCP_KEY_SECRET_LEGACY 旧盐，仅 verify 兜底；轮换盐时配旧盐即可让存量 key 免于重发

MCP Server 不生成 Key（不需要 generate_mcp_key），只校验。
纯 stdlib 实现，零 jonex_core 导入（避免 pydantic v1/v2 冲突）。
"""
import hashlib
import hmac
import logging
import os

logger = logging.getLogger("mcp")

# 两个公开默认值（与主项目 jonex_core/common/security_guards.py 逐字一致，改动需同步）：
# 前者是 config.py 的 pydantic 字段默认（52 字符），后者是 deploy/.env.example 模板值
# （72 字符）。两者不是同一个串，只判其中一个必然漏掉另一条路径。
_INSECURE_JWT_SECRETS = frozenset(
    {
        "your_jwt_secret_key_here_please_change_in_production",
        "your_jwt_secret_key_here_change_in_production_please_use_a_strong_secret",
    }
)

# 本地开发逃生阀；故意不依赖环境名判定（生产 ENV 未显式设置时默认 "dev"）。
_ESCAPE_HATCH_ENV = "ALLOW_INSECURE_JWT_SECRET"
_ESCAPE_HATCH_TRUE = frozenset({"1", "true", "yes", "on"})

#: 判定为「生产」的 ENV 取值，与 ``jonex_core/common/security_guards.py`` 同一政策。
_PRODUCTION_ENVS = frozenset({"prod", "production"})


def _is_production_env() -> bool:
    """``ENV=prod`` / ``production`` 才算生产；未设置按非生产处理。"""
    return os.getenv("ENV", "").strip().lower() in _PRODUCTION_ENVS


def assert_secure_jwt_secret() -> None:
    """校验 JWT_SECRET 已配置为真实随机值，否则拒绝启动（fail-closed）。

    与 ``jonex_core/common/security_guards.py`` 逻辑一致；此处独立实现是为了维持
    MCP Server 零 jonex_core 依赖（避免 pydantic v1/v2 冲突）。

    MCP Server 是 MCP Key 的**校验侧**，secret 与写入侧（platform 服务）不一致时
    所有 Key 都会验不过——轮换 JWT_SECRET 时 ``deploy/.env`` 与 ``deploy/.env.mcp``
    必须同批修改。

    两档判据**分档不同**（2026-10-15 对齐 security_guards）：

    - **空值**：任何环境都拒绝启动 —— 空 secret 不是任何一种环境的合法配置，
      且 ``_current_hash_secret`` 随后也会拒绝（纵深防御）；
    - **仓库内公开默认值**：只有生产档（``ENV=prod``）拒绝启动，其余环境打一条
      warning 后放行（用户口径：非生产画像沿用公开值本来就是预期）。

    Raises:
        RuntimeError: JWT_SECRET 为空（任何环境），或生产档下仍为仓库内公开的默认值。
    """
    if os.getenv(_ESCAPE_HATCH_ENV, "").strip().lower() in _ESCAPE_HATCH_TRUE:
        logger.warning(
            "%s 已启用，跳过 JWT_SECRET 安全校验——仅限本地开发，生产环境严禁设置。",
            _ESCAPE_HATCH_ENV,
        )
        return

    value = os.getenv("JWT_SECRET", "").strip()
    if not value:
        raise RuntimeError(
            "JWT_SECRET 不安全，拒绝启动：未配置或为空。"
            "MCP Server 用它对 MCP Key 做 HMAC 校验。"
            "请在 .env.mcp 中设置为与 deploy/.env 一致的真实随机值；"
            f"若确需在本地开发沿用默认值，显式设置 {_ESCAPE_HATCH_ENV}=1。"
        )

    # 公开默认值判定：2026-09-22 曾整段停用（当时与 security_guards 同步停用），
    # 10-14 security_guards 已恢复、本文件漏了 —— 10-15 补回，并按生产档分档。
    if value in _INSECURE_JWT_SECRETS:
        message = (
            "JWT_SECRET 不安全：仍为仓库内公开的默认值。"
            "MCP Server 用它对 MCP Key 做 HMAC 校验，使用公开默认值意味着任何持有该"
            "公开值的一方都能推导出校验逻辑，且与写入侧 platform 服务必然分叉。"
            "请在 .env.mcp 中设置为与 deploy/.env 一致的真实随机值。"
        )
        if _is_production_env():
            raise RuntimeError(f"{message}（生产档 ENV=prod，拒绝启动）")
        logger.warning(
            "JWT_SECRET 启动守卫已跳过（ENV=%s，非生产档，只告警不拦）：%s"
            "⚠️ 若这是对外实例，请显式设置 ENV=prod —— 不设时这一层保护不会生效。",
            os.getenv("ENV", "").strip() or "<未设置>",
            message,
        )

MCP_KEY_SECRET_ENV = "MCP_KEY_SECRET"
MCP_KEY_SECRET_LEGACY_ENV = "MCP_KEY_SECRET_LEGACY"


def _current_hash_secret() -> bytes:
    """当前 HMAC 盐：优先 MCP_KEY_SECRET，未配置回退 JWT_SECRET（兼容旧部署）。

    从环境变量直接读取（不 import Settings 类，per D-02）。

    Raises:
        RuntimeError: 两个环境变量均未配置或为空（纵深防御——虽然 app.py 启动守卫会拦截，
                    但作为独立模块不应静默退化）
    """
    secret = os.getenv(MCP_KEY_SECRET_ENV, "").strip() or os.getenv("JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            f"{MCP_KEY_SECRET_ENV} 与 JWT_SECRET 环境变量均未配置或为空，"
            f"无法安全计算 MCP Key 哈希。请设置 {MCP_KEY_SECRET_ENV} 环境变量（推荐）或 JWT_SECRET。"
        )
    return secret.encode()


def _legacy_hash_secret() -> bytes | None:
    """旧盐，仅用于 verify 兜底；未配置返回 None。"""
    secret = os.getenv(MCP_KEY_SECRET_LEGACY_ENV, "").strip()
    return secret.encode() if secret else None


def _hmac_hex(secret: bytes, plaintext: str) -> str:
    return hmac.new(secret, plaintext.encode(), hashlib.sha256).hexdigest()


def hash_mcp_key(plaintext: str) -> str:
    """对 MCP Key 明文做 HMAC-SHA256 哈希。

    Args:
        plaintext: MCP Key 明文（如 yxm_abc123...）

    Returns:
        64 字符 hex 哈希值
    """
    return _hmac_hex(_current_hash_secret(), plaintext)


def candidate_hashes(plaintext: str) -> list[str]:
    """返回该明文在全部候选盐下的哈希列表，当前盐优先。

    MCP Server 的鉴权是「按 key_hash 查表」（``WHERE key_hash = $1``），不是
    「取出哈希再比对」——所以盐轮换的双盐兜底必须落在**查询侧**：调用方逐个
    候选查一次，任一命中即可。仅调用 :func:`verify_mcp_key` 兜不住存量 Key，
    它没有生产调用点。

    Args:
        plaintext: MCP Key 明文（如 yxm_abc123...）

    Returns:
        list[str]: 当前盐哈希在前；若配置了 MCP_KEY_SECRET_LEGACY 且其哈希与
            当前盐不同，则追加 legacy 哈希。空明文返回空列表（不查库）。

    Raises:
        RuntimeError: 两个盐均未配置（见 _current_hash_secret）。
    """
    if not plaintext:
        return []
    candidates = [_hmac_hex(_current_hash_secret(), plaintext)]
    legacy = _legacy_hash_secret()
    if legacy is not None:
        legacy_hash = _hmac_hex(legacy, plaintext)
        # 两个盐配成同一个值时去重，避免重复查库
        if legacy_hash != candidates[0]:
            candidates.append(legacy_hash)
    return candidates


def verify_mcp_key(plaintext: str, stored_hash: str) -> bool:
    """校验 MCP Key 明文与存储的哈希是否匹配。

    先试当前盐，未命中再试 MCP_KEY_SECRET_LEGACY（盐轮换的平滑过渡）。
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
    if hmac.compare_digest(_hmac_hex(_current_hash_secret(), plaintext), stored_hash):
        return True
    legacy = _legacy_hash_secret()
    return legacy is not None and hmac.compare_digest(_hmac_hex(legacy, plaintext), stored_hash)
