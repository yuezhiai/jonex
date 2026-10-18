# -*- coding: utf-8 -*-
"""[jonex] 编译任务的 PG 存储（P2）。

形状对齐 raganything `TaskRepository`（save/get），但落 PG 而非 JSON 文件。
自建 engine（不复用平台 `jonex_core.common.database`）—— 与现有引擎容器自建 DB 层的做法一致，保持编译服务的存储边界独立。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .task_model import CompileTask

logger = logging.getLogger(__name__)

# ⚠️ 这张表是**白名单**：`save()` 只落 `_COLUMNS` 里的列、`get()` 只从 `SELECT *` 的行
# 构造模型。模型上有、这里没有的字段会被**静默丢掉**（写不进去、读回来是默认值）——
# 不报错、不告警，只在「代次栅栏」这类读回值的地方表现为「莫名恒为 0」。
# 新增模型字段时必须同步这里，并同步 `deploy/postgres/update/` 里的建列脚本。
_COLUMNS = (
    "task_id", "tenant_id", "kb_id", "document_id", "name", "file_path",
    "status", "current_step", "progress", "ontology_status", "ontology_schema",
    "ontology_data", "ontology_error", "config_snapshot", "timeline",
    "error_code", "error_message", "content_generation",
    "created_at", "updated_at", "started_at", "completed_at",
)

# JSONB 列：裸 text() 无法从列推断参数类型，必须显式 bindparam(type_=JSONB)，
# 否则 asyncpg 按文本编码 dict/list 报 "'dict' object has no attribute 'encode'"。
_JSON_COLUMNS = ("ontology_schema", "ontology_data", "config_snapshot", "timeline")


def resolve_database_url() -> Optional[str]:
    """编译任务库的连接串：`ONTOLOGY_COMPILE_DATABASE_URL` 优先，回落 `DATABASE_URL`。

    单独抽出供测试的 skip 守卫复用——否则守卫只查 `DATABASE_URL`，而
    `from_env()` 优先前者：只配 `ONTOLOGY_COMPILE_DATABASE_URL` 时会出现
    「测试 skip、store 其实可用」的绿但未验证。无值返回 None。
    """
    return os.getenv("ONTOLOGY_COMPILE_DATABASE_URL") or os.getenv("DATABASE_URL")


class CompileTaskStore:
    """编译任务存储。所有方法 async。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], engine=None) -> None:
        self._sf = session_factory
        self._engine = engine

    @classmethod
    def from_env(cls) -> "CompileTaskStore":
        url = resolve_database_url()
        if not url:
            raise RuntimeError(
                "缺少 ONTOLOGY_COMPILE_DATABASE_URL / DATABASE_URL，无法建任务存储"
            )
        engine = create_async_engine(url, pool_pre_ping=True)
        return cls(async_sessionmaker(engine, expire_on_commit=False), engine=engine)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def save(self, task: CompileTask) -> None:
        row = task.dict()
        # ⚠️ JSONB 列必须换成 **JSON 安全值**再绑：pydantic v1 的 `.dict()` 保留嵌套模型与
        # `datetime` **对象**（`timeline` 的元素是 `StageTiming`，其 start/end 是 datetime），
        # 而 JSONB 绑定要求可 json.dumps ⇒ 否则
        # `StatementError: Object of type datetime is not JSON serializable`。
        #
        # 失效形态很隐蔽（2026-10-17 真机验收实测）：`timeline` **为空**时 `.dict()` 出 `[]`、
        # 照样存得下 ⇒ **入队成功、一进执行就全挂**（worker 每轮 save 都炸，任务永远落不了终态）。
        #
        # 只替换 JSON 列、其余列仍用 `.dict()`：`created_at`/`updated_at` 要留给 asyncpg 当
        # timestamp 绑，用 `.json()` 会把它们变成 ISO 字符串反而对不上类型。
        #
        # ⚠️ `include` 必须收窄到 JSON 列：`task.json()` 会序列化**全部字段**——包括
        # **不落库**的 `engine_chunks`（台账实测 15–20MB；14.6MB 时 `task.json()` 0.72s
        # + `json.loads()` 0.06s、额外峰值内存 ~34MB；每任务保存 3–4 次且**同步阻塞
        # 事件循环**，worker 与 HTTP 同进程）。同时这也让 `extra="allow"` 挂上的额外属性
        # 不再进入序列化：将来有人挂一个不可 JSON 编码的对象，不会变成「每次 save 都失败」。
        _json_row = json.loads(task.json(include=set(_JSON_COLUMNS)))
        for _c in _JSON_COLUMNS:
            row[_c] = _json_row.get(_c)
        cols = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        # updated_at 由下面显式 `now()` 赋值，不能再出现在 EXCLUDED 列表里
        # （否则 PG 报 "multiple assignments to same column"）。
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in ("task_id", "updated_at")
        )
        sql = text(
            f"INSERT INTO ontology_compile.compile_tasks ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT (task_id) DO UPDATE SET {updates}, updated_at = now()"
        ).bindparams(*(bindparam(c, type_=JSONB) for c in _JSON_COLUMNS))
        async with self._sf() as session:
            await session.execute(sql, {c: row.get(c) for c in _COLUMNS})
            await session.commit()

    async def get(self, task_id: str) -> Optional[CompileTask]:
        async with self._sf() as session:
            res = await session.execute(
                text("SELECT * FROM ontology_compile.compile_tasks WHERE task_id = :tid"),
                {"tid": task_id},
            )
            row = res.mappings().first()
        return CompileTask(**dict(row)) if row else None

    async def list_recent(self, tenant_id: str, kb_id: str, limit: int = 20) -> list[CompileTask]:
        async with self._sf() as session:
            res = await session.execute(
                text(
                    "SELECT * FROM ontology_compile.compile_tasks "
                    "WHERE tenant_id = :t AND kb_id = :k ORDER BY created_at DESC LIMIT :n"
                ),
                {"t": tenant_id, "k": kb_id, "n": limit},
            )
            rows = res.mappings().all()
        return [CompileTask(**dict(r)) for r in rows]

    async def delete(self, task_id: str) -> bool:
        """删除一条任务。返回是否真的删掉了（不存在时 False，**不抛**）。"""
        async with self._sf() as session:
            res = await session.execute(
                text("DELETE FROM ontology_compile.compile_tasks WHERE task_id = :tid"),
                {"tid": task_id},
            )
            await session.commit()
            return (res.rowcount or 0) > 0

    async def list_by_status(
        self, status: str, *, tenant_id: Optional[str] = None,
        kb_id: Optional[str] = None, limit: int = 100,
    ) -> list["CompileTask"]:
        """按状态捞任务（重启续跑 / 看门狗用）。`tenant_id`/`kb_id` 可选，用于收窄范围。"""
        sql = "SELECT * FROM ontology_compile.compile_tasks WHERE status = :st"
        params: dict = {"st": status, "n": limit}
        if tenant_id is not None:
            sql += " AND tenant_id = :t"
            params["t"] = tenant_id
        if kb_id is not None:
            sql += " AND kb_id = :k"
            params["k"] = kb_id
        sql += " ORDER BY created_at DESC LIMIT :n"
        async with self._sf() as session:
            res = await session.execute(text(sql), params)
            rows = res.mappings().all()
        return [CompileTask(**dict(r)) for r in rows]

    async def claim_one(self, status: str = "queued") -> Optional["CompileTask"]:
        """**原子认领**一条待跑任务（置 `processing` 并返回它）；没有则返回 None。

        ⚠️ 必须一步完成：worker 默认 `concurrency=2`，且同一行可能被多个容器同时看到 ——
        「SELECT 再 save」两步会让两个协程取到同一条，**同一编译任务跑两遍**
        （重复写 LightRAG / Neo4j）。故用 `UPDATE … RETURNING *` 做 CAS，
        并以 `FOR UPDATE SKIP LOCKED` 让并发认领者跳过被锁行而不是排队。
        """
        async with self._sf() as session:
            res = await session.execute(
                text(
                    "UPDATE ontology_compile.compile_tasks "
                    "SET status = 'processing', started_at = now(), updated_at = now() "
                    "WHERE task_id = ("
                    "  SELECT task_id FROM ontology_compile.compile_tasks "
                    "  WHERE status = :st ORDER BY created_at LIMIT 1 "
                    "  FOR UPDATE SKIP LOCKED"
                    ") RETURNING *"
                ),
                {"st": status},
            )
            row = res.mappings().first()
            await session.commit()
        return CompileTask(**dict(row)) if row else None


_STORE: Optional[CompileTaskStore] = None


def get_task_store() -> CompileTaskStore:
    """进程内单例（编译服务容器用）。"""
    global _STORE
    if _STORE is None:
        _STORE = CompileTaskStore.from_env()
    return _STORE
