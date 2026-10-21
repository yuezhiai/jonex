"""知识库保留策略扫描调度器（第 3 期 · 任务 5/8）。

「闹钟 + 电池」里的闹钟：按 `RETENTION_SCHEDULER_INTERVAL_SECONDS` 周期扫两批
候选，交给 `DocumentService` 的归档 / 销毁方法执行。形状对齐
`capabilities/platform/services/identity_sync_scheduler.py`（单例 + `_loop` +
`run_once()` 公开给单测），**不依赖任务中心**。

## 一轮干两件事

1. **归档候选**：`is_deleted=0`、状态不在归档/删除/处理中、且**未被任务中心接管**
   的文档，按三级策略（document → kb → tenant → `RETENTION_DEFAULT_DAYS`）解析
   保留天数，`created_at + retention_days <= LOCALTIMESTAMP` 即到期 →
   `archive_document`。
2. **销毁候选**：`status='archived'` 且 `archived_at` 非空，按三级策略解析宽限
   天数，`archived_at + grace_days <= LOCALTIMESTAMP` →
   `destroy_document(system=True)`。

两条候选 SQL 都排除任务中心在途文档（`task_center_documents.state IN
('submitting', 'managed')`）：这些文档归任务中心管，旧对账/调度不得替它们发起
归档或销毁（同 `_not_task_center_managed` 的口径）；`released` 不排除 —— 它只
表示看门狗交还，交还后没有别人会动这份文档，保留策略必须能接管。

## 公平性（大租户积压不饿死其它租户）

只按全局 `ORDER BY created_at LIMIT batch` 取，一个大租户的历史积压会长期占满
每一批，其它租户的到期文档永远排不进来。所以先在内层用
`row_number() OVER (PARTITION BY tenant_id ORDER BY created_at)` 给每个租户的
候选编号，外层层 `rn <= :per_tenant_limit` 截断，再全局排序取
`LIMIT :batch`。每租户上限 `max(1, batch // 10)`：默认 200 条时每租户每轮最多
20 条，10 个租户就能吃满一批；租户很少时按全局批次仍有上限。

## 时间口径（必须 SQL 侧）

`knowledge_documents.created_at` / `archived_at` 是 **naive 本地时间**
（`TimestampMixin` 用 `datetime.now()` 落库）。所以到期判据一律用
**`LOCALTIMESTAMP`**、在 SQL 里做加法 —— 不能取回 Python 再用 `datetime.now()`
比对：两边一旦跨时区/夏令时或数据库与进程时钟有偏差，就会出现「刚上传就被归档」
或「永远不到期」。

⚠️ **DB TimeZone 前提**：`LOCALTIMESTAMP` 返回**数据库会话时区**的本地时间。
本模块假定数据库会话时区与写入 `created_at` 的应用进程本地时区一致（部署上
同机/同 TZ；`created_at` 由 Python `datetime.now()` 写入）。若两者不一致，到期
判定会整体偏移（偏快 = 提前归档，偏慢 = 延迟归档）—— 这是部署约束，不在代码里
做时区换算（表里存的就是 naive 本地时间，没有可换算的偏移量）。

## 参数全部绑定

`COALESCE(...)` 的全局默认值、每租户上限、`LIMIT` 的批次上限全部走
`:default_days` / `:default_grace` / `:per_tenant_limit` / `:batch` 绑定参数。
保留天数虽然来自配置（不是用户输入），但绑定是这类批量 SQL 的默认姿势：值变了
不用重建语句，也不会把配置错误变成 SQL 语法问题。

## 失败隔离与进程内退避

单篇文档归档/销毁抛异常（索引清理失败、状态被并发推进、对象存储抖动……）
只记一条带 tenant/doc 的 WARNING，**同轮其余文档继续**。失败文档在
`RETENTION_FAILURE_COOLDOWN_SECONDS`（默认 3600 秒）内不再尝试，避免每轮重复
撞同一个错误；连续失败次数只用于日志，不做持久化。

⚠️ 冷却文档在 **SQL 里就排除**（`NOT (d.id = ANY(:cooling_ids))`，空集合传
`[]`），而不是取回 Python 再 `continue`：否则一篇永久失败的文档每轮仍占着一个
「每租户窗口」名额，同租户其它到期文档可能永远排不进来（与任务中心接管文档
必须下沉成 `NOT EXISTS` 是同一个理由）。

⚠️ **单副本假设**：退避表是**进程内** `dict`（`_failures`），不共享、不落库、
重启即清空。多副本部署时每个副本各记各的，同一篇失败文档可能被另一个副本重试
一次；这不会造成数据损坏，因为归档/销毁本身是幂等的（归档状态机重复归档直接
返回成功；销毁对已删除行返回幂等成功）。要做跨副本退避需要持久化表，本期不做
（评审明示「不做持久化失败退避表」）。

## 停止语义

`stop()` 会 `cancel()` 后台循环任务并等待它退出；若恰好在 `run_once` 中途，
该轮可能被取消在「已归档 A、还没归档 B」之间。这是安全的：两篇文档的状态机
各自独立，下一轮会继续处理 B；归档/销毁本身幂等，重复执行不会产生第二条记录。

## 轮末统计

`run_once()` 返回 `{"archived", "destroyed", "scanned", "failed"}` 并在轮末打一条
统一 INFO 日志。`scanned` = 本轮从两条 SQL 取回的候选行数（含因冷却被跳过的），
`failed` = 本轮归档/销毁抛异常的次数（冷却跳过不计失败）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncContextManager, Callable, Optional

from sqlalchemy import ARRAY, String, bindparam, text

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session

from ..models.task_center import ACTIVE_STATES

logger = logging.getLogger(__name__)

#: 已交由任务中心接管的在途状态。**从单一事实来源派生**（模型里的 `ACTIVE_STATES`
#: = submitting / managed），不要在这里重抄字面量 —— 漂移时调度器会开始碰正在被
#: 任务中心处理的文档。`released` 刻意不在内（它只表示看门狗交还，见模块 docstring）。
_TASK_CENTER_IN_FLIGHT = "(" + ", ".join(f"'{state}'" for state in ACTIVE_STATES) + ")"

#: `NOT (d.id = ANY(:cooling_ids))` 的数组绑定类型。显式声明让 asyncpg 在
#: **空数组**时也能确定参数类型（否则 `[]` 的推断依赖语句上下文，容易在线上才炸）。
_COOLING_IDS_PARAM = bindparam("cooling_ids", type_=ARRAY(String))

#: 归档候选（公平性截断见模块 docstring）。
_ARCHIVE_CANDIDATES_SQL = text(
    f"""
    SELECT t.id, t.tenant_id, t.knowledge_base_id
    FROM (
        SELECT d.id, d.tenant_id, d.knowledge_base_id, d.created_at,
               row_number() OVER (PARTITION BY d.tenant_id ORDER BY d.created_at) AS rn
        FROM knowledge_base.knowledge_documents d
        LEFT JOIN data_governance.retention_policy dp
          ON dp.tenant_id = d.tenant_id AND dp.scope_type = 'document' AND dp.scope_id = d.id
        LEFT JOIN data_governance.retention_policy kp
          ON kp.tenant_id = d.tenant_id AND kp.scope_type = 'kb' AND kp.scope_id = d.knowledge_base_id
        LEFT JOIN data_governance.retention_policy tp
          ON tp.tenant_id = d.tenant_id AND tp.scope_type = 'tenant' AND tp.scope_id = ''
        WHERE d.is_deleted = 0
          AND d.status NOT IN ('archived', 'deleting', 'deleted', 'pending', 'parsing', 'ingesting')
          AND NOT (d.id = ANY(:cooling_ids))
          AND NOT EXISTS (
              SELECT 1 FROM knowledge_base.task_center_documents tc
              WHERE tc.document_id = d.id
                AND tc.tenant_id = d.tenant_id
                AND tc.state IN {_TASK_CENTER_IN_FLIGHT}
          )
          AND COALESCE(dp.retention_days, kp.retention_days, tp.retention_days, :default_days) > 0
          AND d.created_at + make_interval(days => COALESCE(dp.retention_days, kp.retention_days, tp.retention_days, :default_days)) <= LOCALTIMESTAMP
    ) t
    WHERE t.rn <= :per_tenant_limit
    ORDER BY t.created_at
    LIMIT :batch
    """
).bindparams(_COOLING_IDS_PARAM)

#: 销毁候选。宽限期按同一套三级策略解析；`grace_days >= 0` 让负值成为
#: 「不自动销毁」的显式语义（负宽限 = 永不销毁），与 0（立即可销毁）区分开。
_DESTROY_CANDIDATES_SQL = text(
    f"""
    SELECT t.id, t.tenant_id, t.knowledge_base_id
    FROM (
        SELECT d.id, d.tenant_id, d.knowledge_base_id, d.archived_at,
               row_number() OVER (PARTITION BY d.tenant_id ORDER BY d.archived_at) AS rn
        FROM knowledge_base.knowledge_documents d
        LEFT JOIN data_governance.retention_policy dp
          ON dp.tenant_id = d.tenant_id AND dp.scope_type = 'document' AND dp.scope_id = d.id
        LEFT JOIN data_governance.retention_policy kp
          ON kp.tenant_id = d.tenant_id AND kp.scope_type = 'kb' AND kp.scope_id = d.knowledge_base_id
        LEFT JOIN data_governance.retention_policy tp
          ON tp.tenant_id = d.tenant_id AND tp.scope_type = 'tenant' AND tp.scope_id = ''
        WHERE d.is_deleted = 0
          AND d.status = 'archived'
          AND d.archived_at IS NOT NULL
          AND NOT (d.id = ANY(:cooling_ids))
          AND NOT EXISTS (
              SELECT 1 FROM knowledge_base.task_center_documents tc
              WHERE tc.document_id = d.id
                AND tc.tenant_id = d.tenant_id
                AND tc.state IN {_TASK_CENTER_IN_FLIGHT}
          )
          AND COALESCE(dp.grace_days, kp.grace_days, tp.grace_days, :default_grace) >= 0
          AND d.archived_at + make_interval(days => COALESCE(dp.grace_days, kp.grace_days, tp.grace_days, :default_grace)) <= LOCALTIMESTAMP
    ) t
    WHERE t.rn <= :per_tenant_limit
    ORDER BY t.archived_at
    LIMIT :batch
    """
).bindparams(_COOLING_IDS_PARAM)

#: 系统动作的审计用户名（归档/销毁的 user_id 为空，但 username 要能看出是谁干的）。
SYSTEM_ACTOR_NAME = "retention-scheduler"

#: 每租户每轮上限的下限（`batch // 10` 在 batch < 10 时为 0，必须抬到 1）。
_MIN_PER_TENANT_LIMIT = 1


def _default_document_service_factory() -> Any:
    """默认文档服务工厂（惰性 import，避免模块导入期的循环依赖）。"""
    from .document_service import DocumentService

    return DocumentService()


class RetentionScheduler:
    """保留策略后台循环：扫到期归档 → 扫宽限到期销毁。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        batch: Optional[int] = None,
        default_days: Optional[int] = None,
        default_grace: Optional[int] = None,
        failure_cooldown: Optional[float] = None,
        session_factory: Optional[Callable[[], AsyncContextManager[Any]]] = None,
        document_service_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        config = get_config()
        self._interval = float(
            interval
            if interval is not None
            else getattr(config, "RETENTION_SCHEDULER_INTERVAL_SECONDS", 3600)
        )
        self._batch = int(
            batch
            if batch is not None
            else getattr(config, "RETENTION_SCAN_BATCH", 200)
        )
        self._default_days = int(
            default_days
            if default_days is not None
            else getattr(config, "RETENTION_DEFAULT_DAYS", 0)
        )
        self._default_grace = int(
            default_grace
            if default_grace is not None
            else getattr(config, "RETENTION_GRACE_DAYS", 30)
        )
        self._failure_cooldown = float(
            failure_cooldown
            if failure_cooldown is not None
            else getattr(config, "RETENTION_FAILURE_COOLDOWN_SECONDS", 3600)
        )
        # 两个注入口：单测替换真实 DB 与真实文档服务。
        self._session_factory = session_factory or get_db_session
        self._document_service_factory = (
            document_service_factory or _default_document_service_factory
        )
        # 进程内失败退避：doc_id -> {"count": 连续失败次数, "until": monotonic 截止}
        # ⚠️ 单副本假设，详见模块 docstring。
        self._failures: dict[str, dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """后台循环是否在跑（未启动 / 已停止 / 已退出都返回 False）。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """起循环，返回是否处于运行态。

        - `interval <= 0`：显式关闭，返回 False，不起后台任务；
        - 已在跑：幂等，返回 True；
        - `batch <= 0`：仍启动但每轮不会处理任何文档，打一条 WARNING（通常表示
          配置失误，静默空转很难被发现）。
        """
        if self._interval <= 0:
            logger.info("[Retention] 调度器已关闭（RETENTION_SCHEDULER_INTERVAL_SECONDS<=0）")
            return False
        if self.is_running:
            return True
        if self._batch <= 0:
            logger.warning(
                "[Retention] RETENTION_SCAN_BATCH=%s（<=0）：调度器将空转，"
                "每轮不会处理任何文档",
                self._batch,
            )
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[Retention] 保留扫描调度器已启动（间隔 %.1fs，每批最多 %s 条，"
            "默认保留 %s 天 / 宽限 %s 天）",
            self._interval,
            self._batch,
            self._default_days,
            self._default_grace,
        )
        return True

    async def stop(self) -> None:
        """停循环（未起过就什么都不做）。

        `cancel()` 后等待任务退出；若取消落在 `run_once` 中途，本轮剩余文档
        留给下一轮（归档/销毁幂等），见模块 docstring「停止语义」。
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._failures.clear()
            logger.info("[Retention] 保留扫描调度器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败不该让循环死掉，下一轮接着来
                logger.warning(
                    "[Retention] 本轮出错，跳过本轮：%s", exc, exc_info=True,
                )

    # ── 一轮 ──────────────────────────────────────────────

    async def run_once(self) -> dict:
        """跑一轮：归档候选 → 销毁候选；返回本轮统计。单测直接调，不必等定时器。"""
        batch = max(0, self._batch)
        per_tenant_limit = max(_MIN_PER_TENANT_LIMIT, batch // 10)
        base_params = {
            "per_tenant_limit": per_tenant_limit,
            "batch": batch,
            # 冷却中的文档在 SQL 里排除，避免占「每租户窗口」名额。
            # 空集合也必须显式绑定 `[]`（见 `_COOLING_IDS_PARAM`）。
            "cooling_ids": self._cooling_ids(),
        }

        archive_rows = await self._fetch(
            self.archive_candidates_statement(),
            {**base_params, "default_days": self._default_days},
        )
        service: Any = None

        def _service() -> Any:
            # 零候选（或全部处于失败冷却）时不构造 DocumentService —— 它是无状态
            # 的，但构造会带起一轮依赖导入，空转轮没必要付这个成本。
            nonlocal service
            if service is None:
                service = self._document_service_factory()
            return service

        archived = 0
        failed = 0
        for row in archive_rows:
            doc_id = row.id
            if self._in_failure_cooldown(doc_id):
                logger.debug(
                    "[Retention] 归档冷却中，跳过 tenant=%s doc=%s",
                    row.tenant_id,
                    doc_id,
                )
                continue
            try:
                await _service().archive_document(
                    row.tenant_id,
                    doc_id,
                    row.knowledge_base_id,
                    reason="retention",
                    username=SYSTEM_ACTOR_NAME,
                )
                self._clear_failure(doc_id)
                archived += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不能拖垮整轮
                failed += 1
                self._record_failure(doc_id)
                logger.warning(
                    "[Retention] 归档失败 tenant=%s doc=%s（连续失败 %d 次，"
                    "冷却 %.0fs）：%s",
                    row.tenant_id,
                    doc_id,
                    self._failures[doc_id]["count"],
                    self._failure_cooldown,
                    exc,
                )

        # 销毁候选在归档之后查：宽限 0 的文档可在同一轮被归档并立即销毁。
        destroy_rows = await self._fetch(
            self.destroy_candidates_statement(),
            {**base_params, "default_grace": self._default_grace},
        )

        destroyed = 0
        for row in destroy_rows:
            doc_id = row.id
            if self._in_failure_cooldown(doc_id):
                logger.debug(
                    "[Retention] 销毁冷却中，跳过 tenant=%s doc=%s",
                    row.tenant_id,
                    doc_id,
                )
                continue
            try:
                await _service().destroy_document(
                    row.tenant_id,
                    doc_id,
                    row.knowledge_base_id,
                    system=True,
                    username=SYSTEM_ACTOR_NAME,
                )
                self._clear_failure(doc_id)
                destroyed += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不能拖垮整轮
                failed += 1
                self._record_failure(doc_id)
                logger.warning(
                    "[Retention] 销毁失败 tenant=%s doc=%s（连续失败 %d 次，"
                    "冷却 %.0fs）：%s",
                    row.tenant_id,
                    doc_id,
                    self._failures[doc_id]["count"],
                    self._failure_cooldown,
                    exc,
                )

        stats = {
            "archived": archived,
            "destroyed": destroyed,
            "scanned": len(archive_rows) + len(destroy_rows),
            "failed": failed,
        }
        # 轮末统一日志（含全部统计口径；空转轮也留一条，便于确认调度器还活着）。
        logger.info(
            "[Retention] 本轮：扫描 %d，归档 %d，销毁 %d，失败 %d",
            stats["scanned"],
            stats["archived"],
            stats["destroyed"],
            stats["failed"],
        )
        return stats

    async def _fetch(self, stmt, params: dict) -> list:
        async with self._session_factory() as session:
            result = await session.execute(stmt, params)
            return list(result.all())

    # ── 失败退避（进程内）────────────────────────────────

    def _cooling_ids(self) -> list[str]:
        """当前仍在冷却期内的 doc id 列表（作为 SQL 数组参数）。"""
        now = time.monotonic()
        return [
            doc_id
            for doc_id, entry in self._failures.items()
            if now < entry["until"]
        ]

    def _in_failure_cooldown(self, doc_id: str) -> bool:
        entry = self._failures.get(doc_id)
        return bool(entry) and time.monotonic() < entry["until"]

    def _record_failure(self, doc_id: str) -> None:
        entry = self._failures.setdefault(doc_id, {"count": 0, "until": 0.0})
        entry["count"] += 1
        entry["until"] = time.monotonic() + max(0.0, self._failure_cooldown)

    def _clear_failure(self, doc_id: str) -> None:
        self._failures.pop(doc_id, None)

    # ── SQL 语句（独立成方法：单测可直接断言 SQL 形状）──────────

    @staticmethod
    def archive_candidates_statement():
        return _ARCHIVE_CANDIDATES_SQL

    @staticmethod
    def destroy_candidates_statement():
        return _DESTROY_CANDIDATES_SQL


# ============================================================
# 进程内单例（能力模块 initialize / shutdown 时用）
# ============================================================

_scheduler: Optional[RetentionScheduler] = None


def get_scheduler() -> RetentionScheduler:
    """取本进程的保留扫描调度器（首次调用时创建）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = RetentionScheduler()
    return _scheduler


async def start_scheduler() -> Optional[RetentionScheduler]:
    """起调度器；`interval <= 0` 时返回 None（显式关闭）。

    ⚠️ 不要在这里读 `scheduler._task` —— 用 `start()` 的返回值 / `is_running`
    判断运行态（私有字段随时可能变）。
    """
    scheduler = get_scheduler()
    return scheduler if await scheduler.start() else None


async def stop_scheduler() -> None:
    """停调度器（未起过就什么都不做）。"""
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


__all__ = [
    "RetentionScheduler",
    "SYSTEM_ACTOR_NAME",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
