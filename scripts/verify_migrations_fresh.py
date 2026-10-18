#!/usr/bin/env python3
"""在一次性 PostgreSQL 容器里验证「全新库」迁移路径。

做的事：
1. 起一个空的 ``postgres:16-alpine`` 容器（不挂卷，退出即删）；
2. 把 ``deploy/postgres/migrations/*.sql`` 拷进 ``/docker-entrypoint-initdb.d/``，
   让官方 entrypoint 按文件名字母序执行（与生产首次初始化同路径）；
3. 检查 010–014 建出的表/列/约束都在；
4. 再把 010–014 逐个人工重跑一遍，验证幂等；
5. 用该库跑 ``TEST_FILES`` 里的集成测试，
   让仓储 SQL 在真实 PostgreSQL 上执行（而非只断言 SQL 文本）。

用法（仓库根目录）::

    uv run --with pytest --with pytest-asyncio \
        python scripts/verify_migrations_fresh.py

需要本机 Docker 可用。脚本不会触碰任何既有容器/数据库。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "deploy" / "postgres" / "migrations"
TEST_FILES = (
    "tests/integration/test_standard_catalog_repository_pg.py",
    "tests/integration/test_kb_delete_cleanup_pg.py",
    "tests/integration/test_clearance_repository_pg.py",
    "tests/integration/test_classification_access_pg.py",
)
IMAGE = os.getenv("DG_MIGRATION_TEST_IMAGE", "postgres:16-alpine")
NAME = f"jonex-mig-verify-{os.getpid()}"
DB_USER = "jonex"
DB_PASSWORD = "change-me"
DB_NAME = "jonex"
IDEMPOTENT_FILES = (
    "010_data_governance.sql",
    "011_lifecycle_retention.sql",
    "012_kb_type_check.sql",
    "013_classification.sql",
    "014_user_clearance.sql",
)


class VerifyError(RuntimeError):
    pass


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=ROOT, text=True, capture_output=capture, env=env,
    )
    if check and proc.returncode != 0:
        detail = (proc.stdout or "") + (proc.stderr or "")
        raise VerifyError(f"命令失败（{proc.returncode}）：{' '.join(cmd)}\n{detail}")
    return proc


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", *args], check=check)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def psql(sql: str, *, check: bool = True) -> str:
    proc = docker(
        "exec", NAME, "psql", "-U", DB_USER, "-d", DB_NAME,
        "-v", "ON_ERROR_STOP=1", "-tAc", sql,
        check=check,
    )
    return (proc.stdout or "").strip()


def wait_until_initialized(deadline_seconds: int = 120) -> None:
    """等 entrypoint 跑完初始化，再等**正式服务**可查 013 的表。

    官方镜像的流程是「临时库跑 initdb.d → 停临时库 → 用同一数据目录正式启动」。
    直接在 init 脚本刚建完表时查询会撞上临时库停机窗口（`database system is
    shutting down`），所以先等日志出现 `PostgreSQL init process complete`，
    再轮询正式服务。
    """
    deadline = time.time() + deadline_seconds
    init_done = False
    last = ""
    while time.time() < deadline:
        if not init_done:
            logs = docker("logs", "--tail", "200", NAME, check=False)
            text_logs = docker_logs(logs)
            if "PostgreSQL init process complete" in text_logs:
                init_done = True
            else:
                time.sleep(1)
                continue
        proc = docker(
            "exec", NAME, "psql", "-U", DB_USER, "-d", DB_NAME,
            "-tAc",
            "SELECT to_regclass('data_governance.classification_level') IS NOT NULL",
            check=False,
        )
        last = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0 and last == "t":
            return
        time.sleep(2)
    logs = docker("logs", "--tail", "120", NAME, check=False)
    raise VerifyError(f"迁移初始化超时：{last}\n{docker_logs(logs)}")


def docker_logs(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def expect_sql(label: str, sql: str, expected: str) -> None:
    actual = psql(sql)
    if actual != expected:
        raise VerifyError(f"{label} 不符合预期：期望 {expected!r}，实际 {actual!r}")


def check_target_state() -> None:
    expect_sql(
        "classification_level 表",
        "SELECT to_regclass('data_governance.classification_level') IS NOT NULL",
        "t",
    )
    expect_sql(
        "classification_category 表",
        "SELECT to_regclass('data_governance.classification_category') IS NOT NULL",
        "t",
    )
    expect_sql(
        "classification_default 表",
        "SELECT to_regclass('data_governance.classification_default') IS NOT NULL",
        "t",
    )
    expect_sql(
        "asset_metadata.classification_categories 列",
        """
        SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'data_governance'
           AND table_name = 'asset_metadata'
           AND column_name = 'classification_categories'
        """,
        "1",
    )
    expect_sql(
        "旧 sensitivity CHECK 已移除",
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'data_governance.asset_metadata'::regclass
           AND conname = 'ck_asset_metadata_sensitivity'
        """,
        "0",
    )
    expect_sql(
        "分类数组 CHECK 存在",
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'data_governance.asset_metadata'::regclass
           AND conname = 'ck_asset_metadata_classification_categories'
        """,
        "1",
    )
    expect_sql(
        "user_clearance 表",
        "SELECT to_regclass('data_governance.user_clearance') IS NOT NULL",
        "t",
    )
    expect_sql(
        "user_clearance 唯一约束存在",
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'data_governance.user_clearance'::regclass
           AND conname = 'uq_user_clearance_user'
        """,
        "1",
    )
    expect_sql(
        "user_clearance level_code CHECK 存在",
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'data_governance.user_clearance'::regclass
           AND conname = 'ck_user_clearance_level_code'
        """,
        "1",
    )
    expect_sql(
        "user_clearance tenant 索引存在",
        """
        SELECT count(*) FROM pg_indexes
         WHERE schemaname = 'data_governance'
           AND tablename = 'user_clearance'
           AND indexname = 'ix_user_clearance_tenant'
        """,
        "1",
    )
    expect_sql(
        "kb_type CHECK 存在",
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'knowledge_base.knowledge_info'::regclass
           AND conname = 'ck_knowledge_info_kb_type'
        """,
        "1",
    )


def rerun_incremental_migrations() -> None:
    for filename in IDEMPOTENT_FILES:
        docker(
            "exec", NAME, "psql", "-U", DB_USER, "-d", DB_NAME,
            "-v", "ON_ERROR_STOP=1",
            "-f", f"/docker-entrypoint-initdb.d/{filename}",
        )


def main() -> int:
    if not MIGRATIONS.is_dir():
        raise VerifyError(f"迁移目录不存在：{MIGRATIONS}")
    sql_files = sorted(MIGRATIONS.glob("*.sql"))
    if not sql_files:
        raise VerifyError(f"迁移目录里没有 .sql：{MIGRATIONS}")

    port = free_port()
    print(f"[verify] 起容器 {NAME}（镜像 {IMAGE}，端口 127.0.0.1:{port}）")
    docker("rm", "-f", NAME, check=False)
    ok = False
    try:
        docker(
            "create", "--name", NAME,
            "-e", f"POSTGRES_USER={DB_USER}",
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}",
            "-e", f"POSTGRES_DB={DB_NAME}",
            "-p", f"127.0.0.1:{port}:5432",
            IMAGE,
        )
        # 用 docker cp 而不是 -v：Windows 下宿主机路径可能含中文/盘符，
        # 绑定挂载不如复制稳妥；文件在容器首次启动前就位即可。
        for sql_file in sql_files:
            docker(
                "cp", str(sql_file),
                f"{NAME}:/docker-entrypoint-initdb.d/{sql_file.name}",
            )
        docker("start", NAME)
        print("[verify] 等待全量迁移完成 ...")
        wait_until_initialized()
        check_target_state()
        print("[verify] 全量迁移目标态 OK")

        print("[verify] 重跑 010–014 验证幂等 ...")
        rerun_incremental_migrations()
        check_target_state()
        print("[verify] 幂等 OK")

        dsn = (
            f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}"
            f"@127.0.0.1:{port}/{DB_NAME}"
        )
        env = {**os.environ, "DG_TEST_DATABASE_URL": dsn}
        print("[verify] 跑真实 PG 集成测试 ...")
        run(
            [sys.executable, "-m", "pytest", *TEST_FILES, "-q"],
            env=env,
        )
        print("[verify] 全部通过")
        ok = True
        return 0
    finally:
        if not ok:
            logs = docker("logs", "--tail", "120", NAME, check=False)
            print("[verify] 容器日志（末 120 行）：", file=sys.stderr)
            print(docker_logs(logs), file=sys.stderr)
        docker("rm", "-f", NAME, check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerifyError as exc:
        print(f"[verify] 失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
