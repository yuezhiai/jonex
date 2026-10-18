#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""单独更新 MCP Key / ingest key 的 HMAC 盐 ``MCP_KEY_SECRET``。

为什么单独一个脚本（不并入 ``rotate_secrets.py``）::

    ``MCP_KEY_SECRET`` 是**验证用的盐**，换掉它会让存量 key 失效 —— 那是「有意为之的
    破坏性操作」，不能混进 ``make init`` 的补齐路径（否则初始化时会意外作废线上 key）。
    本脚本只做一件事：把一个 profile 下所有 env 文件里的 ``MCP_KEY_SECRET`` 换成
    **同一个**新随机值。

    盐只有一个，跨文件必须同值：``platform`` 签发 key、``mcp-server`` 验签，两处盐
    不一致会出现「刚签发的 key 立刻验不过」。故脚本按 profile 整组写入，并在只给了
    单个文件、而同一目录还有别的文件带该键时**报警**（防止漏改一半）。

轮换后果（脚本运行后也会打印）::

    - ``MCP_KEY_SECRET`` 为空时，盐回退 ``JWT_SECRET``（见 common/crypto.py）。
    - ingest key（``yxk_*``）：payload 是 base64 明文 ⇒ 「已知盐 + 已知明文」可伪造，
      所以 **只认当前盐、不做 legacy 兜底** ⇒ 轮换后**必须重新下发**。
    - MCP Key（``yxm_*``）：明文是高熵随机串 ⇒ 无伪造风险，verify 会额外尝试
      ``MCP_KEY_SECRET_LEGACY`` ⇒ 想免于重发，把该变量设为「轮换前生效的盐」。
      本脚本**不动** ``MCP_KEY_SECRET_LEGACY``（它是过渡态，不是待轮换的密钥）。

用法::

    # 预览（不写文件）
    scripts/security/rotate_mcp_salt.py --profile deploy/env/tencent --dry-run

    # 实际更新（默认先备份，后缀 .bak.<UTC 时间戳>）
    scripts/security/rotate_mcp_salt.py --profile deploy/env/tencent

    # 只处理单个文件（不推荐：同目录兄弟文件需自行同步）
    scripts/security/rotate_mcp_salt.py deploy/.env
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import secrets
import shutil
import sys
from pathlib import Path

KEY = "MCP_KEY_SECRET"
LEGACY_KEY = "MCP_KEY_SECRET_LEGACY"


def _is_env_file(p: Path) -> bool:
    """收 ``.env*`` 与 ``env.*``（57 画像用无点命名 ``env.<profile>.57``），排除模板与备份。"""
    n = p.name
    if ".bak." in n or n.endswith(".example"):
        return False
    return n.startswith(".env") or n.startswith("env.")


def _resolve(a) -> list[Path]:
    if a.profile:
        d = Path(a.profile)
        if not d.is_dir():
            print(f"✗ 不是目录: {d}", file=sys.stderr)
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and _is_env_file(p))
    return [Path(a.envfile)] if a.envfile else []


def _line_index(lines: list[str], key: str) -> int | None:
    """返回 ``key=`` **活动行**的下标；注释行不算。"""
    for i, line in enumerate(lines):
        if not line or line[0] in "# \t" or "=" not in line:
            continue
        if line.split("=", 1)[0].strip() == key:
            return i
    return None


def run(paths: list[Path], *, dry_run: bool, backup: bool) -> int:
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("✗ 没有可处理的 env 文件", file=sys.stderr)
        return 2

    targets: list[tuple[Path, list[str], int]] = []
    for p in paths:
        lines = p.read_text(encoding="utf-8").split("\n")
        i = _line_index(lines, KEY)
        if i is not None:
            targets.append((p, lines, i))

    if not targets:
        print(
            f"✗ {len(paths)} 个文件里都没有活动的 {KEY}= 行；请先在对应 env 文件补上该键，"
            f"或确认 --profile 指向了正确的画像目录。",
            file=sys.stderr,
        )
        return 2

    # 单文件模式下，若同目录兄弟文件也带该键，提示会被漏改（盐必须全局同值）。
    if len(targets) == 1:
        lonely = targets[0][0]
        siblings = sorted(
            q
            for q in lonely.parent.iterdir()
            if q.is_file()
            and q != lonely
            and _is_env_file(q)
            and _line_index(q.read_text(encoding="utf-8").split("\n"), KEY) is not None
        )
        if siblings:
            print(
                "🔴 只处理了一个文件，但同目录还有其它文件带 "
                f"{KEY}：\n" + "".join(f"     - {q}\n" for q in siblings) +
                "   盐必须跨文件同值，否则「刚签发的 key 立刻验不过」。"
                "建议改用 --profile 指向该目录。\n",
                file=sys.stderr,
            )

    before_nonempty = {
        lines[i].split("=", 1)[1].strip() for _p, lines, i in targets
    } - {""}

    new = secrets.token_urlsafe(48)
    fp = hashlib.sha256(new.encode()).hexdigest()[:12]

    print(f"待更新 {KEY} 的文件（{len(targets)} 个）:")
    for p, _lines, _i in targets:
        print(f"  · {p}")
    if len(before_nonempty) > 1:
        print(f"  ⚠️ 当前存在 {len(before_nonempty)} 个不同取值（本就不一致），本次统一为新盐。")
    elif not before_nonempty:
        print("  ℹ️ 当前全为空 ⇒ 实际生效的盐是回退值 JWT_SECRET；本次改为独立盐。")

    if dry_run:
        print(f"\n[dry-run] 未写入。新盐指纹 sha256[:12]={fp}，长度 {len(new)}。")
    else:
        if backup:
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for p, _lines, _i in targets:
                shutil.copy2(p, p.with_name(f"{p.name}.bak.{stamp}"))
            print(f"\n已备份 {len(targets)} 个文件（后缀 .bak.{stamp}）")
        for p, lines, i in targets:
            lines[i] = f"{KEY}={new}"
            p.write_text("\n".join(lines), encoding="utf-8")
        print(f"✔ 已更新 {len(targets)} 个文件，新盐指纹 sha256[:12]={fp}（不打印盐值）")

    print(
        "\n🔴 后果：存量 key 立即失效，需重新签发 ——\n"
        "   · ingest key(yxk_*) 只认当前盐、无 legacy 兜底 ⇒ api_push 入站推送会 401，必须重发；\n"
        f"   · MCP Key(yxm_*) 若要让存量 key 继续可用，请把 {LEGACY_KEY} 设为\n"
        "     「本次轮换前生效的盐」（旧值请从备份或密码库取），本脚本不动该变量。\n"
        "   生效方式：重启 platform 与 mcp-server（以及任何签发 ingest key 的服务）。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="更新 MCP Key / ingest key 的 HMAC 盐 MCP_KEY_SECRET（按 profile 跨文件同值）"
    )
    ap.add_argument("envfile", nargs="?", type=Path, help="单个 env 文件")
    ap.add_argument("--profile", type=Path, help="profile 目录（处理其下全部 env 文件）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--no-backup", action="store_true", help="不备份（默认会备份）")
    a = ap.parse_args()
    if not a.envfile and not a.profile:
        ap.error("需要 envfile 或 --profile")
    return run(_resolve(a), dry_run=a.dry_run, backup=not a.no_backup)


if __name__ == "__main__":
    sys.exit(main())
