#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""画像体检：判定一个环境画像是否满足「渗透测试修复」的上线前提（**只读，不改任何文件**）。

为什么单独一个脚本：修复落地到某个环境时，真正的失败模式不是「漏改某一项」，而是
**跨文件不一致**与**会静默失效的派生值** —— 例如 `REDIS_PASSWORD` 设了但兄弟文件的
`REDIS_URL` 没跟（部署后 NOAUTH）、`.env.mcp` 的 `INTERNAL_API_KEY` 与主文件漂移
（MCP 全工具 401）、B1 能力容器缺 `JWT_SECRET`（镜像一重建 `/invoke` 就 403，而容器
仍然 healthy）。这些跑一次就能全查出来，不该靠人肉 review。

公开值黑名单**直接复用** ``rotate_secrets.PUBLIC_DEFAULTS``，避免两处维护漂移。

用法::

    scripts/security/audit_profile.py --profile deploy/env/tencent
    scripts/security/audit_profile.py --profile deploy/env/<profile>
    scripts/security/audit_profile.py --profile deploy --dev      # 本地 dev 画像，放宽 ENV/MCP_KEY_SECRET

退出码：0 = 无 🔴；1 = 存在 🔴（该环境不具备上线前提）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_secrets as R  # noqa: E402  （复用 PUBLIC_DEFAULTS / _load / _lookup）

OK, WARN, BAD = "✅", "🟡", "🔴"

# 必须存在、非空、且不能是公开值的键
REQUIRED_SECRETS = [
    "JWT_SECRET",
    "GATEWAY_API_KEY",
    "INTERNAL_API_KEY",
    "KEYCLOAK_CLIENT_SECRET",
    "DB_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "LLMGW_INTERNAL_TOKENS",
]
# 必须显式存在的键（缺了会静默降级到别的东西）
REQUIRED_PRESENT = {
    "MCP_KEY_SECRET": "缺 ⇒ HMAC 盐回退 JWT_SECRET，轮换 JWT_SECRET 会连带作废全部 ingest/MCP key",
    "DATA_SOURCE_SECRET_KEY": "缺 ⇒ Fernet 主密钥从 JWT_SECRET 派生，轮换 JWT_SECRET 会让库里已加密凭据全部解不开",
}
# 跨文件必须同值的键（GROUPS 之外还要查的派生/对齐项）
EXTRA_SAME_VALUE = [
    "REDIS_URL",
    "LIGHTRAG_API_KEY",
    "DATA_SOURCE_SECRET_KEY",
    "MCP_KEY_SECRET",
    # 同一份凭据被多个容器消费：任一处漂移都会让该容器连不上或 401
    "JWT_SECRET",
    "GATEWAY_API_KEY",
    "INTERNAL_API_KEY",
    "DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "NEO4J_PASSWORD",
    "KEYCLOAK_CLIENT_SECRET",
    "JONEX_WIKI_API_KEY",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
]
# 「键名不同但必须同值」的组 —— 逐键比对**查不出来**，必须单独校验
CROSS_KEY_GROUPS = [
    # LightRAG 的 PGKVStorage / PGDocStatusStorage 连的是**同一个** jonex 库
    ["DB_PASSWORD", "POSTGRES_PASSWORD"],
    # 同一把 lightrag 引擎 key，两个键名（三处出现）
    ["LIGHTRAG_API_KEY"],
]
# B1 能力容器（跑 start_capability.py）必须自带的键
B1_KEYS = ["JWT_SECRET", "JWT_ALGORITHM", "REDIS_URL"]
B1_HINT = "缺 ⇒ 镜像重建后 /invoke 一律 403 code=3004，而容器仍 healthy（健康检查看不出来）"


def _pick_main(files: list[Path]) -> Path | None:
    """主配置：优先精确 `.env`，否则 `.env.<画像目录名>`。"""
    for p in files:
        if p.name == ".env":
            return p
    suffix = f".env.{files[0].parent.name}" if files else ""
    for p in files:
        if p.name == suffix:
            return p
    return files[0] if files else None


def _all_values(data, key: str) -> dict[str, set[str]]:
    """{文件名: {该文件里该键的非空取值}}。"""
    out: dict[str, set[str]] = {}
    for p, value in R._lookup(data, key):
        if value:
            out.setdefault(p.name, set()).add(value)
    return out


def run(paths: list[Path], *, dev: bool, verbose: bool) -> int:
    files = sorted(p for p in paths if p.is_file())
    if not files:
        print(f"{BAD} 没有可检查的 env 文件", file=sys.stderr)
        return 1

    data = R._load(files)
    names = {p.name for p in files}
    main = _pick_main(files)
    findings: list[tuple[str, str, str]] = []  # (level, 标题, 详情)

    print(f"画像目录: {files[0].parent}")
    print(f"文件({len(files)}): {', '.join(sorted(names))}\n")

    # ── 1. 文件清单 ────────────────────────────────────────────────
    suffix = "" if main and main.name == ".env" else f".{files[0].parent.name}"
    expected = {"mcp", "rag", "openkb", "jonex_wiki"}
    missing = [k for k in sorted(expected) if f".env.{k}{suffix}" not in names]
    if missing:
        findings.append((WARN, "文件清单", f"缺 {', '.join('.env.' + m + suffix for m in missing)}（compose 的 env_file 缺文件会直接起不来；make init 可从 .example 补，但补完必须核对共享凭据）"))

    # ── 2. ENV=prod（RC-E：测试 token 后门）─────────────────────────
    env_vals = _all_values(data, "ENV")
    env_v = next(iter({v for vs in env_vals.values() for v in vs}), "")
    if dev:
        findings.append((OK, "ENV", f"dev 画像，ENV={env_v or '(空=dev)'}"))
    elif env_v == "prod":
        findings.append((OK, "ENV=prod", "测试 token 后门已关闭"))
    else:
        findings.append((BAD, "ENV", f"当前 {env_v or '(未配置)'} ⇒ 落进代码默认 dev ⇒ `Bearer jonex_test_<任意租户>` 即成通行证；且 2026-10-15 起 JWT_SECRET/GATEWAY_API_KEY/INTERNAL_API_KEY 的启动守卫**只在 ENV=prod 硬拦**，不设 ENV 时那几层也只剩告警 ⇒ 一份沿用公开默认值的配置会**静默启动**。必须在主文件（及各应用服务读到的文件）显式设 ENV=prod"))

    # ── 3. 必须非空且非公开的密钥 ─────────────────────────────────
    for key in REQUIRED_SECRETS:
        vals = _all_values(data, key)
        if not vals:
            findings.append((BAD, key, "所有文件都没有该键（或为空）"))
            continue
        public = sorted({v for vs in vals.values() for v in vs if R._is_public_or_empty(key, v)})
        if public:
            findings.append((BAD, key, f"仍是**公开默认值**（{len(public)} 个）⇒ 启动守卫会拒绝启动 / 或可被伪造"))
        else:
            findings.append((OK, key, f"{len(vals)} 个文件有值，均非公开"))

    # ── 4. 必须显式存在（否则静默降级）──────────────────────────────
    for key, why in REQUIRED_PRESENT.items():
        vals = _all_values(data, key)
        if dev and key == "MCP_KEY_SECRET":
            findings.append((OK, key, f"dev 画像，允许缺（{why}）") if not vals else (OK, key, "已配置"))
        elif vals:
            findings.append((OK, key, f"{len(vals)} 个文件有值"))
        else:
            findings.append((BAD, key, why))

    # ── 5. 跨文件同值 ─────────────────────────────────────────────
    same_value_keys = list(R._GROUP_MEMBERS) + EXTRA_SAME_VALUE
    for key in sorted(set(same_value_keys)):
        vals = _all_values(data, key)
        distinct = {v for vs in vals.values() for v in vs}
        if len(distinct) > 1:
            who = ", ".join(f"{n}({len(vs)})" for n, vs in sorted(vals.items()))
            findings.append((BAD, f"{key} 跨文件不一致", f"{len(distinct)} 个不同值 | {who}"))

    # ── 5b. 跨键名同值（DB_PASSWORD ≡ POSTGRES_PASSWORD 等）──────────
    for group in CROSS_KEY_GROUPS:
        per = {k: _all_values(data, k) for k in group}
        present = {k: v for k, v in per.items() if v}
        if len(present) < 2:
            findings.append((OK, f"{' = '.join(group)} 同值", "仅有 1 个键出现，无需比对"))
            continue
        # ⚠️ per 是 {键: {文件名: {取值}}}，要展平两层才是取值本身
        flat = {v for by_file in present.values() for vs in by_file.values() for v in vs}
        if len(flat) > 1:
            who = "; ".join(f"{k}@{','.join(sorted(vs))}" for k, vs in present.items())
            findings.append((BAD, f"{' = '.join(group)} 取值不一致",
                             f"必须同值（否则其中一方连不上）| {who}"))
        else:
            findings.append((OK, f"{' = '.join(group)} 同值", f"{len(present)} 个键取值一致"))

    # ── 6. Redis：口令与 URL 必须自洽（本轮实际踩到）────────────────
    rp = {v for vs in _all_values(data, "REDIS_PASSWORD").values() for v in vs}
    if rp:
        bad = sorted(
            n
            for n, vs in _all_values(data, "REDIS_URL").items()
            if not any("@" in v for v in vs)
        )
        if bad:
            findings.append((BAD, "REDIS_URL 缺口令", f"REDIS_PASSWORD 非空，但 {', '.join(bad)} 的 REDIS_URL 无 `@` ⇒ NOAUTH（mcp-server 限流存储、B1 容器心跳全部失败）"))
        else:
            findings.append((OK, "REDIS_URL↔REDIS_PASSWORD", "已带上口令"))
    else:
        findings.append((OK, "REDIS_URL↔REDIS_PASSWORD", "未设口令（Redis 无鉴权；端口不对公网开放时可接受）"))

    # ── 7. llm-gateway：消费方 token 必须都在白名单内 ───────────────
    allow: set[str] = set()
    for vs in _all_values(data, "LLMGW_INTERNAL_TOKENS").values():
        for v in vs:
            allow |= {t.strip() for t in v.split(",") if t.strip()}
    if allow:
        outside: dict[str, set[str]] = {}
        for p, (lines, idx) in data.items():
            for k, i in idx.items():
                if k.endswith("_BINDING_API_KEY") or k == "LLMGW_INTERNAL_TOKENS":
                    v = R._value_of(lines[i])
                    if v and k != "LLMGW_INTERNAL_TOKENS" and v not in allow:
                        outside.setdefault(k, set()).add(p.name)
        if outside:
            findings.append((BAD, "llm-gw 令牌不在白名单", "; ".join(f"{k}@{','.join(sorted(v))}" for k, v in sorted(outside.items())) + " ⇒ 调用 401"))
        else:
            findings.append((OK, "llm-gw 令牌白名单", f"{len(allow)} 个 token，消费方全部命中"))
    else:
        findings.append((WARN, "llm-gw 令牌白名单", "LLMGW_INTERNAL_TOKENS 为空 ⇒ gateway fail-closed 返回 503（不是漏配就是故意的）"))

    # ── 8. B1 能力容器必须自带三个键 ──────────────────────────────
    for k in ("jonex_wiki", "openkb"):
        f = next((p for p in files if p.name in (f".env.{k}{suffix}", f".env.{k}")), None)
        if f is None:
            continue
        lines, idx = data[f]
        lack = [key for key in B1_KEYS if key not in idx or not R._value_of(lines[idx[key]])]
        if lack:
            findings.append((BAD, f"{f.name} 缺 {'/'.join(lack)}", B1_HINT))
        else:
            findings.append((OK, f"{f.name} 自带 {', '.join(B1_KEYS)}", "镜像重建安全"))

    # ── 9. MinIO：ROOT 与旧名必须一致 ─────────────────────────────
    for root_key, old_key in (("MINIO_ROOT_USER", "MINIO_ACCESS_KEY"), ("MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY")):
        rv = {v for vs in _all_values(data, root_key).values() for v in vs}
        ov = {v for vs in _all_values(data, old_key).values() for v in vs}
        if rv and ov and not (rv & ov):
            findings.append((WARN, f"{root_key} ≠ {old_key}", "compose 已改用 ROOT 派生给 milvus；旧名仅历史兼容，但两值不同容易误判实际凭据"))
    if not dev:
        mu = {v for vs in _all_values(data, "MINIO_ROOT_USER").values() for v in vs}
        if mu & {"minioadmin"}:
            findings.append((BAD, "MinIO 仍是 minioadmin", "公开默认凭据"))

    # ── 10. MCP_SERVER_PUBLIC_URL（端口收敛后 :8002 在 prod 不通）──
    mcp_urls = {v for vs in _all_values(data, "MCP_SERVER_PUBLIC_URL").values() for v in vs}
    if mcp_urls:
        for u in sorted(mcp_urls):
            if ":8002" in u or (not dev and "127.0.0.1" in u):
                findings.append((WARN, "MCP_SERVER_PUBLIC_URL", f"{u} ⇒ prod 不发布 8002，WorkBuddy 连不上（应为 http(s)://<对外地址>/mcp）"))
    else:
        findings.append((WARN, "MCP_SERVER_PUBLIC_URL", "未配置 ⇒ 下发给 WorkBuddy 的 MCP 地址为空，客户端连不上"))

    # ── 11. 参考项（不判级）──────────────────────────────────────
    kb_hosts = {v for vs in _all_values(data, "KB_INGESTION_ALLOWED_HOSTS").values() for v in vs}
    findings.append((OK, "KB_INGESTION_ALLOWED_HOSTS", f"{'已配置' if kb_hosts else '未配置（空 = 拒绝所有私网出站，是安全默认）'}"))

    # ── 输出 ──────────────────────────────────────────────────────
    order = {BAD: 0, WARN: 1, OK: 2}
    findings.sort(key=lambda t: order.get(t[0], 9))
    for lvl, title, detail in findings:
        print(f"  {lvl} {title}")
        if detail and (lvl != OK or verbose):
            print(f"      {detail}")
    n_bad = sum(1 for lvl, _, _ in findings if lvl == BAD)
    n_warn = sum(1 for lvl, _, _ in findings if lvl == WARN)
    print(f"\n结论: 🔴 {n_bad} 项 / 🟡 {n_warn} 项 / 检查项 {len(findings)}")
    if n_bad:
        print(f"{BAD} 该画像不具备上线前提，先修 🔴 项")
    return 1 if n_bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="画像体检：渗透测试修复的上线前提（只读）")
    ap.add_argument("--profile", type=Path, required=True, help="画像目录，如 deploy/env/tencent")
    ap.add_argument("--dev", action="store_true", help="本地 dev 画像：放宽 ENV 与 MCP_KEY_SECRET")
    ap.add_argument("-v", "--verbose", action="store_true", help="连通过项也打印详情")
    a = ap.parse_args()
    d = a.profile
    if not d.is_dir():
        print(f"{BAD} 不是目录: {d}", file=sys.stderr)
        return 1
    files = sorted(
        p
        for p in d.iterdir()
        if p.is_file() and (p.name.startswith(".env") or p.name.startswith("env."))
        and ".bak." not in p.name and not p.name.endswith(".example")
    )
    return run(files, dev=a.dev, verbose=a.verbose)


if __name__ == "__main__":
    sys.exit(main())
