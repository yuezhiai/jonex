#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""环境文件密钥生成 / 轮换工具（支持**按 profile 整组、跨文件同值**轮换）。

为什么需要「整组」：
    一个部署画像不是单个 `.env`，而是**一组**文件。以 `deploy/env/tencent/` 为例::

        .env.tencent                    JWT_SECRET GATEWAY_API_KEY INTERNAL_API_KEY MINIO_* LLMGW_INTERNAL_TOKENS
        .env.mcp.tencent                JWT_SECRET INTERNAL_API_KEY     <- 必须与 .env.tencent 同值
        .env.openkb.tencent             JWT_SECRET
        .env.jonex_wiki.tencent         JWT_SECRET

    只改 `.env.tencent` 会造成**同值假设破裂**：例如某些服务仍发旧的
    `GATEWAY_API_KEY`，被 Gateway 拒绝；mcp-server 仍发旧 `INTERNAL_API_KEY`，
    MCP 知识库工具全部 401。故轮换必须以 profile 为单位、跨文件写入同一个新值。

两种用途：

1. ``--fill-missing``：只把**空值或仓库内公开默认值**换成随机值（`make init` 用）。
   组内只要出现「公开/空」，且**同一键在所有文件里都**是公开/空，才整组一起换；
   若混有自定义值则跳过并告警（避免把本来一致的一组改成不一致）。
   默认**不碰 DB_PASSWORD**（改 env 不会改库里的真实口令，会连不上库）。

2. ``--rotate``：无条件轮换。``--backup`` 先备份每个文件。

设计要点：

- **同值组**（``GROUPS``）：组内键共享同一个新值，跨全部文件。
  ``LLMGW_INTERNAL_TOKENS`` 是服务端白名单，各 ``*_BINDING_API_KEY`` 是客户端凭据；
  MinIO 的 ``ROOT_*`` 与 ``ACCESS_KEY``/``SECRET_KEY`` 同源。
- **不打印任何密钥值**，只打印键名/文件名/长度。
- ``DB_PASSWORD`` 属 ``ROTATE_REQUIRES_OPT_IN``：须配 ``--allow-db-password``，
  且**必须另行 ALTER USER**。

用法::

    scripts/security/rotate_secrets.py --fill-missing deploy/.env
    scripts/security/rotate_secrets.py --rotate --backup --profile deploy/env/tencent
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

# 仓库内曾公开过的值（与 jonex_core/common/security_guards.py 保持同步）
PUBLIC_DEFAULTS: dict[str, set[str]] = {
    "JWT_SECRET": {
        "your_jwt_secret_key_here_please_change_in_production",
        "your_jwt_secret_key_here_change_in_production_please_use_a_strong_secret",
        # 兄弟模板（.env.openkb / .env.jonex_wiki .example）
        # 用的是这个占位值。若不算哨兵，整键会被判成「各文件取值不一致，需人工确认」
        # 而被**整体跳过** → deploy/.env 的 JWT_SECRET 会留在空值上。
        "change-me-must-match-platform-deploy-env",
    },
    "INTERNAL_API_KEY": {
        "change-me-in-production",
    },
    "GATEWAY_API_KEY": {
        "change-me-in-production",
        "jonex_test_gateway",
        # `.env.ontology_compile.example` 的占位值（已注释说明「必须与 deploy/.env
        # 逐值一致」）。不算哨兵时，它与主 `.env` 的空值会被判成「各文件取值不一致」
        # ⇒ fill-missing 整键跳过 ⇒ 新环境 ontology-compile 出站时 401。
        "change-me-must-match-platform-deploy-env",
    },
    "KEYCLOAK_CLIENT_SECRET": {
        "change-me-in-production",
        "jonex-platform-public-pkce-client",
    },
    "MINIO_ROOT_PASSWORD": {"change-me-in-production", "minioadmin"},
    "MINIO_ACCESS_KEY": {"minioadmin"},
    "MINIO_SECRET_KEY": {"minioadmin"},
    "MINIO_ROOT_USER": {"minioadmin"},
    "REDIS_PASSWORD": {"change-me-in-production"},
    "DB_PASSWORD": {"change-me-in-production"},
    # llm-gateway 令牌组：公开的测试令牌
    "LLMGW_INTERNAL_TOKENS": {"gw_lightrag,gw_platform,gw_openkb,gw_jonex_wiki"},
    # 注意：这里必须穷举 OLD_LLMGW_TOKENS 里的**全部**公开 token。少一个（曾漏
    # gw_openkb / gw_jonex_wiki），兄弟模板里用该 token 的键就会被判成「自定义值」，
    # 进而让整个令牌组以「组内部分键是自定义值」为由被冻结、白名单留在公开值上。
    "LLM_BINDING_API_KEY": {"gw_lightrag", "gw_platform", "gw_openkb", "gw_jonex_wiki"},
    "EMBEDDING_BINDING_API_KEY": {"gw_lightrag", "gw_platform", "gw_openkb", "gw_jonex_wiki"},
    "RERANK_BINDING_API_KEY": {"gw_lightrag", "gw_platform", "gw_openkb", "gw_jonex_wiki"},
    "VLM_BINDING_API_KEY": {"gw_lightrag", "gw_platform", "gw_openkb", "gw_jonex_wiki"},
}

# 一律轮换的「自包含」密钥（改了不影响外部系统，只需重启本栈）。
# MINIO_* 由其同值组统一处理，故不列此处，避免两处各改一次而分叉。
ROTATE_SELF_CONTAINED = [
    "JWT_SECRET",
    "INTERNAL_API_KEY",
    "GATEWAY_API_KEY",
    "KEYCLOAK_CLIENT_SECRET",
    "REDIS_PASSWORD",
]

# 需显式开启才轮换（有外部系统依赖）
ROTATE_REQUIRES_OPT_IN = ["DB_PASSWORD"]

# fill-missing 模式**不碰**的键（改 env 无法改真实口令/上游配置）
FILL_MISSING_EXCLUDED = {"DB_PASSWORD", "KEYCLOAK_CLIENT_SECRET"}

# llm-gateway 令牌组的**键名**集合（值见 PUBLIC_DEFAULTS）。
# 用于 `--keep-llmgw-tokens`：本地 dev 必须保留公开的 gw_* 不变 ——
# 宿主机调试栈（根级 .env.local + .vscode/launch.json 硬编码）依赖它们，
# 而 .vscode/launch.json 里的那份是写死的，模板承载不了随机 token。
# 生产 profile 不存在这类消费方，故**不要**在生产使用该开关。
LLMGW_KEY_GROUP = frozenset(
    {
        "LLMGW_INTERNAL_TOKENS",
        "LLM_BINDING_API_KEY",
        "EMBEDDING_BINDING_API_KEY",
        "RERANK_BINDING_API_KEY",
        "VLM_BINDING_API_KEY",
        "ONTOLOGY_LLM_BINDING_API_KEY",
        "OPENKB_LLM_API_KEY",
    }
)

# 必须「同值」的键组：轮换时组内共享同一个新 token（跨全部文件）
GROUPS: list[list[str]] = [
    # llm-gateway：服务端白名单 + 各客户端发出的令牌
    [
        "LLMGW_INTERNAL_TOKENS",
        "LLM_BINDING_API_KEY",
        "EMBEDDING_BINDING_API_KEY",
        "RERANK_BINDING_API_KEY",
        "VLM_BINDING_API_KEY",
    ],
    # MinIO：compose 里 milvus 的 MINIO_ACCESS_KEY/SECRET_KEY 派生自 ROOT_*，
    # 而 env 文件里另有同名旧键可能被其他容器读取，故两组必须同值
    ["MINIO_ROOT_USER", "MINIO_ACCESS_KEY"],
    ["MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"],
]

# 上述「必须同值」组里的全部键名。
# fill-missing 的**逐键**路径必须排除它们，否则会出现这个真实缺陷：
# 逐键各生成一个随机值 → 分组检查发现「已非公开值」而整组跳过 → 组内取值分叉。
# 实测后果（2026-10-15，本机生产形态部署）：
#   · MINIO_ROOT_USER ≠ MINIO_ACCESS_KEY ⇒ MinIO 客户端报
#     "The Access Key Id you provided does not exist in our records"
#   · RERANK/VLM/EMBEDDING 等客户端 token 不在白名单内 ⇒ llm-gateway 401
# 分组成员一律只由分组路径处理（组内共享同一新值、跨全部文件）。
_GROUP_MEMBERS = frozenset(k for g in GROUPS for k in g)

_ALNUM = string.ascii_letters + string.digits

# 历史上公开过的 llm-gateway 客户端 token。用于**按值发现**客户端键：
# 只靠键名枚举会漏 —— 实际就漏了 ONTOLOGY_LLM_BINDING_API_KEY /
# OPENKB_LLM_API_KEY（命名不统一，不属于
# *_BINDING_API_KEY 家族），导致轮换白名单后这些调用全部 401。
# 故改为「值 ∈ 已知 llm-gw token 集合」即认定为客户端凭据，与键名无关。
OLD_LLMGW_TOKENS = {
    "gw_lightrag",
    "gw_platform",
    "gw_openkb",
    "gw_jonex_wiki",
}
# 白名单自身不参与同步
_LLMGW_SYNC_EXCLUDE = {"LLMGW_INTERNAL_TOKENS"}


def sync_llmgw_clients(data, changed: list, skipped: list) -> None:
    """不变式：所有 llm-gateway 客户端凭据必须等于当前白名单里的某个 token。

    按**值**发现客户端键（含历史公开 token 与当前白名单 token），因此不受键名不统一影响。
    白名单取值口径：取所有文件里 ``LLMGW_INTERNAL_TOKENS`` 的第一个非空值
    （轮换后各文件已同值）。
    """
    canonical = ""
    for _p, (lines, idx) in data.items():
        if "LLMGW_INTERNAL_TOKENS" in idx:
            for t in _value_of(lines[idx["LLMGW_INTERNAL_TOKENS"]]).split(","):
                if t.strip():
                    canonical = t.strip()
                    break
        if canonical:
            break
    if not canonical:
        skipped.append("llm-gw 客户端同步(白名单为空，跳过)")
        return
    known = set(OLD_LLMGW_TOKENS) | {canonical}
    for p, (lines, idx) in data.items():
        for key, i in list(idx.items()):
            if key in _LLMGW_SYNC_EXCLUDE:
                continue
            cur = _value_of(lines[i])
            # 两类都归一：① 已知失效值（历史公开 token）；
            # ② 该键属于 llm-gw 令牌组、且当前为公开/空值（如模板里的空 OPENKB_LLM_API_KEY）。
            stale = cur in known
            blank_group_key = key in LLMGW_KEY_GROUP and _is_public_or_empty(key, cur)
            if (stale or blank_group_key) and cur != canonical:
                lines[i] = f"{key}={canonical}"
                reason = (
                    "同步为当前白名单 token（原值已失效会 401）"
                    if stale
                    else "填入当前白名单 token（原为公开/空值）"
                )
                changed.append((key, p.name, reason))


def _new_secret(key: str) -> str:
    if key.startswith("MINIO"):
        return "".join(secrets.choice(_ALNUM) for _ in range(32))
    return secrets.token_urlsafe(48)


def _value_of(line: str) -> str:
    return line.split("=", 1)[1].strip() if "=" in line else ""


def _is_public_or_empty(key: str, value: str) -> bool:
    if not value.strip():
        return True
    return value.strip() in PUBLIC_DEFAULTS.get(key, set())


def _load(paths: list[Path]) -> dict[Path, tuple[list[str], dict[str, int]]]:
    data: dict[Path, tuple[list[str], dict[str, int]]] = {}
    for p in paths:
        lines = p.read_text(encoding="utf-8").split("\n")
        idx: dict[str, int] = {}
        for i, l in enumerate(lines):
            if not l or l[0] in "# \t" or "=" not in l:
                continue
            k = l.split("=", 1)[0].strip()
            if k:
                idx[k] = i
        data[p] = (lines, idx)
    return data


def _lookup(data, key: str) -> list[tuple[Path, str]]:
    """返回 [(文件, 当前值)]，仅含真正带该键的文件。"""
    out = []
    for p, (lines, idx) in data.items():
        if key in idx:
            out.append((p, _value_of(lines[idx[key]])))
    return out


def run(
    paths: list[Path],
    *,
    rotate: bool,
    allow_db: bool,
    backup: bool,
    keep_llmgw: bool = False,
    quiet_values: bool = True,
) -> int:
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("✗ 没有可处理的 env 文件", file=sys.stderr)
        return 2

    if backup:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for p in paths:
            shutil.copy2(p, p.with_name(f"{p.name}.bak.{stamp}"))
        print(f"已备份 {len(paths)} 个文件（后缀 .bak.{stamp}）")

    data = _load(paths)
    changed: list[tuple[str, str, str]] = []  # (key, 文件名, 原因)
    skipped: list[str] = []

    def apply(key: str, newv: str, reason: str) -> int:
        n = 0
        for p, (lines, idx) in data.items():
            if key not in idx:
                continue
            i = idx[key]
            if _value_of(lines[i]) == newv:
                continue
            lines[i] = f"{key}={newv}"
            changed.append((key, p.name, reason))
            n += 1
        return n

    if rotate:
        for key in ROTATE_SELF_CONTAINED:
            hits = _lookup(data, key)
            if not hits:
                skipped.append(f"{key}(所有文件中都不存在)")
                continue
            apply(key, _new_secret(key), "轮换（自包含，跨文件同值）")
        for key in ROTATE_REQUIRES_OPT_IN:
            if allow_db:
                if _lookup(data, key):
                    apply(key, _new_secret(key), "轮换（--allow-db-password）")
            elif _lookup(data, key):
                skipped.append(
                    f"{key}(需 --allow-db-password；改 .env 不会改库里的真实口令，"
                    "须另行 ALTER USER，否则应用连不上库)"
                )
        for group in GROUPS:
            if not any(_lookup(data, k) for k in group):
                skipped.append(f"{'/'.join(group)}(所有文件中都不存在)")
                continue
            if keep_llmgw and set(group) & LLMGW_KEY_GROUP:
                skipped.append(
                    f"{'/'.join(group)}(--keep-llmgw-tokens：本地 dev 保留公开 token 组)"
                )
                continue
            shared = _new_secret(group[0])
            for k in group:
                apply(k, shared, "轮换（与同组键共享同一新值）")
    else:
        # fill-missing：只处理「所有出现处都是公开/空」的键
        fillable = [
            k
            for k in PUBLIC_DEFAULTS
            if k not in FILL_MISSING_EXCLUDED
            and k not in _GROUP_MEMBERS
            and not (keep_llmgw and k in LLMGW_KEY_GROUP)
        ]
        for key in fillable:
            hits = _lookup(data, key)
            if not hits:
                continue
            if all(_is_public_or_empty(key, v) for _, v in hits):
                apply(key, _new_secret(key), "由公开默认/空值改为随机值（跨文件同值）")
            elif any(_is_public_or_empty(key, v) for _, v in hits):
                skipped.append(f"{key}(各文件取值不一致，需人工确认，未改动)")
        for group in GROUPS:
            if keep_llmgw and set(group) & LLMGW_KEY_GROUP:
                skipped.append(
                    f"{'/'.join(group)}(--keep-llmgw-tokens：本地 dev 保留公开 token 组)"
                )
                continue
            hits = [(k, p, v) for k in group for (p, v) in _lookup(data, k)]
            if not hits:
                continue
            if all(_is_public_or_empty(k, v) for k, _, v in hits):
                shared = _new_secret(group[0])
                for k in group:
                    apply(k, shared, "由公开默认/空值改为随机值（同组共享）")
            elif any(_is_public_or_empty(k, v) for k, _, v in hits):
                skipped.append(
                    f"{'/'.join(group)}(组内部分键是自定义值，整组未改动以免造成不一致)"
                )
        for key in FILL_MISSING_EXCLUDED:
            if _lookup(data, key):
                skipped.append(f"{key}(有外部系统依赖，fill-missing 不处理)")

    # 不变式：所有 llm-gateway 客户端凭据必须等于当前白名单 token。
    # 无条件执行（只改「已知已失效」的值，不动自定义值），
    # 用于兜住「轮换白名单却漏改某个客户端键」这一类回归。
    if keep_llmgw:
        skipped.append("llm-gw 客户端同步(--keep-llmgw-tokens：令牌组整体保持原样)")
    else:
        sync_llmgw_clients(data, changed, skipped)

    # 不变式：REDIS_URL 里的口令必须与 REDIS_PASSWORD 一致，否则应用连不上 Redis。
    # ⚠️ 必须**跨文件**取口令：通常只有主 .env 声明 `REDIS_PASSWORD`，而各引擎的 env 文件
    # （.env.mcp / .env.openkb / .env.jonex_wiki）只带 `REDIS_URL`。
    # 早先按「本文件同时有这两个键」判断，会整体漏掉它们 —— 表现为 Redis 已开 requirepass，
    # 引擎却仍以无口令 URL 连接而失败。
    _redis_pw = ""
    for _p, (lines, idx) in data.items():
        if "REDIS_PASSWORD" in idx:
            v = _value_of(lines[idx["REDIS_PASSWORD"]])
            if v:
                _redis_pw = v
                break
    if _redis_pw:
        for p, (lines, idx) in data.items():
            if "REDIS_URL" not in idx:
                continue
            i = idx["REDIS_URL"]
            new = re.sub(
                r"^REDIS_URL=redis://(?:[^@/]*@)?redis:6379/0$",
                f"REDIS_URL=redis://:{_redis_pw}@redis:6379/0",
                lines[i],
            )
            if new != lines[i]:
                lines[i] = new
                changed.append(("REDIS_URL", p.name, "同步嵌入 REDIS_PASSWORD"))

    for p, (lines, _) in data.items():
        p.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n{'轮换' if rotate else '补齐'}结果 — {len(paths)} 个文件")
    for p in paths:
        print(f"  · {p}")
    print()
    if changed:
        per_key: dict[str, list[str]] = {}
        for k, f, _ in changed:
            per_key.setdefault(k, []).append(f)
        for k, fs in per_key.items():
            print(f"  ✔ {k:<26} 已更新 {len(fs)} 处  {sorted(fs)}")
    else:
        print("  （无需改动）")
    for s in skipped:
        print(f"  – 跳过 {s}")
    # MCP Key / ingest key 的 HMAC 盐优先取 MCP_KEY_SECRET，为空才回退 JWT_SECRET
    # （jonex_core/common/crypto.py）。故轮换 JWT_SECRET 是否牵连存量 key 取决于它。
    touched = {k for k, _f, _r in changed}
    if "JWT_SECRET" in touched:
        _has_mcp_salt = any(
            _value_of(lines[idx["MCP_KEY_SECRET"]])
            for _p, (lines, idx) in data.items()
            if "MCP_KEY_SECRET" in idx
        )
        if _has_mcp_salt:
            print(
                "\n⚠️ 既有**用户会话**全部失效。MCP Key / ingest key 的盐由 MCP_KEY_SECRET "
                "独立提供，不受本次轮换影响。"
            )
        else:
            print(
                "\n🔴 既有**用户会话**全部失效，且 MCP_KEY_SECRET 为空 ⇒ MCP Key / ingest key "
                "仍以 JWT_SECRET 为 HMAC 盐、存量 key 全部失配：\n"
                "   · ingest key(yxk_*) 只认当前盐、无 legacy 兜底 ⇒ **必须重新下发**；\n"
                "   · MCP Key(yxm_*) 可把 MCP_KEY_SECRET_LEGACY 设为轮换前生效的盐来放行。"
            )

    # 防复发：llm-gateway 令牌除了 compose 栈，还被**宿主机调试栈**消费，
    # 且有一部分写死在 .vscode/launch.json 里（模板无法承载随机 token）。
    # 只改 compose 侧 env 会让「VSCode Debug 跑后端 + 容器跑 llm-gateway」这条链路 401。
    if touched & (set(OLD_LLMGW_TOKENS) | {"LLMGW_INTERNAL_TOKENS"} | {
        "LLM_BINDING_API_KEY", "EMBEDDING_BINDING_API_KEY",
        "RERANK_BINDING_API_KEY", "VLM_BINDING_API_KEY",
        "ONTOLOGY_LLM_BINDING_API_KEY",
        "OPENKB_LLM_API_KEY",
    }):
        extra = [str(f) for f in (Path(".env.local"), Path(".vscode/launch.json")) if f.is_file()]
        if extra:
            print(
                "\n🔴 你改动了 llm-gateway 令牌，但该令牌**还被宿主机调试栈消费**，下列文件未被本工具处理：\n"
                + "".join(f"     - {f}\n" for f in extra)
                + "   它们里的 *_BINDING_API_KEY 若与白名单不一致，"
                "「VSCode Debug 跑后端 + 容器跑 llm-gateway」会全部 401。\n"
                "   两条路选一条：① 把这些键同步成新 token；"
                "② **不要轮换 llm-gw 令牌组**（本地 dev 建议如此），"
                "生产没有这类宿主机消费方，可以轮换。"
            )
    return 0


def _resolve_paths(a) -> list[Path]:
    if a.profile:
        d = Path(a.profile)
        if not d.is_dir():
            print(f"✗ 不是目录: {d}", file=sys.stderr)
            return []
        # 排除备份与示例模板
        return sorted(
            p
            for p in d.iterdir()
            if p.is_file()
            and p.name.startswith(".env")
            and ".bak." not in p.name
            and not p.name.endswith(".example")
        )
    return [Path(a.envfile)] if a.envfile else []


def main() -> int:
    ap = argparse.ArgumentParser(description="环境文件密钥生成 / 轮换（支持 profile 整组）")
    ap.add_argument("envfile", nargs="?", type=Path, help="单个 env 文件")
    ap.add_argument("--profile", type=Path, help="profile 目录（处理其下全部 .env* 文件）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--rotate", action="store_true", help="无条件轮换")
    g.add_argument("--fill-missing", action="store_true", help="只填空值/公开默认值")
    ap.add_argument("--backup", action="store_true", help="处理前备份")
    ap.add_argument(
        "--keep-llmgw-tokens",
        action="store_true",
        help=(
            "仅供本地 dev：保留 llm-gateway 令牌组的公开 gw_* 值不填不轮换。"
            "宿主机调试栈（根级 .env.local 与 .vscode/launch.json）依赖这些值；"
            "生产 profile 没有这类消费方，不要用。"
        ),
    )
    ap.add_argument(
        "--allow-db-password",
        action="store_true",
        help="同时轮换 DB_PASSWORD（危险：须同步改数据库真实口令）",
    )
    a = ap.parse_args()
    if not a.envfile and not a.profile:
        ap.error("需要 envfile 或 --profile")
    paths = _resolve_paths(a)
    return run(
        paths,
        rotate=a.rotate,
        allow_db=a.allow_db_password,
        backup=a.backup,
        keep_llmgw=a.keep_llmgw_tokens,
    )


if __name__ == "__main__":
    sys.exit(main())
