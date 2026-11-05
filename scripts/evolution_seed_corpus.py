#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演化台语料自动播种 CLI（语料治理线 Task 6）。

两种模式：

- `--mode schema`：读该数据源的 active 编译 schema，逐实体生成
  `SELECT COUNT(*) FROM <table>` 题（标识符白名单，SQL 只由固定模板组成）；
- `--mode yaml --file cases.yaml`：导入显式题目。YAML 形状：
  `{"cases": [{name?, question, tier?, expect, control?}]}` 或裸列表。

去重键是归一化问句：同句同内容记 duplicates、同句异内容记 conflicts
（**绝不覆盖**既有语料）；`--dry-run` 只算不写（不建行、不审计、不记版本）。

用法（代码根 /app 或仓库根）：

    python scripts/evolution_seed_corpus.py \\
      --tenant tenant_jonex_demo --kb kb_assets_r2 --ds <ds_id> --mode schema

    python scripts/evolution_seed_corpus.py \\
      --tenant tenant_jonex_demo --kb kb_assets_r2 --ds <ds_id> \\
      --mode yaml --file cases.yaml --dry-run

环境变量与平台进程一致（DB_HOST / DB_PORT / DB_USERNAME / DB_PASSWORD /
DB_NAME）；默认自动加载仓库根 `.env.local`（`--env-file` 可换，
`--no-env-file` 关闭）。结果以 JSON 打印到 stdout。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_TENANT = "tenant_jonex_demo"


def load_env_file(path: str) -> str:
    """加载 shell 风格 env 文件（已存在的环境变量不覆盖）。返回实际加载路径。"""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    return str(file_path)


def load_cases(path: str) -> list:
    """读 YAML 题目文件：`{"cases": [...]}` 或裸列表；其他形状明确报错。"""
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list):
        raise ValueError(
            f"{path} 必须是题目列表，或形如 {{\"cases\": [...]}} 的对象")
    return data


async def run(args: argparse.Namespace) -> dict:
    from jonex_core.common.database import get_db_session

    from capabilities.knowledge_base.services.evolution.corpus_seed import (
        seed,
    )

    mode = "cases" if args.mode == "yaml" else "schema"
    cases = load_cases(args.file) if mode == "cases" else None
    async with get_db_session() as session:
        return await seed(
            session, args.tenant, args.kb, args.ds, mode=mode,
            cases=cases, user_id=args.user_id, dry_run=args.dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="演化台语料自动播种（schema / yaml）")
    parser.add_argument("--tenant", default=DEFAULT_TENANT,
                        help=f"租户 ID（默认 {DEFAULT_TENANT}）")
    parser.add_argument("--kb", required=True, help="知识库 ID")
    parser.add_argument("--ds", required=True, help="数据源 ID")
    parser.add_argument("--mode", required=True, choices=("schema", "yaml"),
                        help="schema=从编译 schema 派生；yaml=导入 --file")
    parser.add_argument("--file", help="YAML 题目文件（--mode yaml 必填）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只算不写（不建行、不审计、不记版本）")
    parser.add_argument("--user-id", default=None,
                        help="审计/版本 created_by（可选）")
    parser.add_argument("--env-file", default=str(_REPO_ROOT / ".env.local"),
                        help="环境变量文件（默认仓库根 .env.local）")
    parser.add_argument("--no-env-file", action="store_true",
                        help="不加载 .env.local")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "yaml" and not args.file:
        parser.error("--mode yaml 必须提供 --file")
    if not args.no_env_file:
        load_env_file(args.env_file)
    try:
        result = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：打印原因并给非零退出码
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
