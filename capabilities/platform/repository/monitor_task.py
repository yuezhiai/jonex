"""平台监控 · 任务监控读接口仓储（T1–T4）。

数据源 = **既有表** ``task_center.tasks`` / ``task_center.subtasks``。

⚠️ Docker 隔离：platform 容器镜像**不含** ``capabilities/task_center/`` 代码
（见 ``deploy/docker/capability.Dockerfile``）⇒ 严禁 ``import capabilities.task_center.*``
（import 必 500）。跨 schema 只读**一律** ``sqlalchemy.text()`` 裸 SQL，写法同
``repository/mcp_service_repository.py``。

取数口径：

- 区间**左闭右开** ``col >= :start AND col < :end``，**禁 ``BETWEEN``**；区间锚点统一
  ``tasks.created_at``（T1–T4 同一锚点 ⇒ 同一任务集合）。
- **时间口径**：``created_at`` / ``started_at`` / ``finished_at`` 都是
  ``TIMESTAMP WITHOUT TIME ZONE``，存的是 **Asia/Shanghai 墙钟的 naive 时间**
  （task_center 写入侧 ``datetime.now()``，容器 ``TZ=Asia/Shanghai``）。本仓储只接收
  service 换算后的 naive 北京值、**不做任何时区换算**；``date_trunc`` 因此直接得到
  北京自然时/日桶，⛔ 不要加 ``AT TIME ZONE``。``as_of`` / ``finished_at`` 由 service 的
  ``_iso`` 补 ``+08:00`` 后返回。
- 假删列是 ``SMALLINT`` ⇒ 统一写 ``is_deleted = 0``（⛔ 非 ``= false``，PG 无
  ``smallint = boolean`` 运算符）；父表 ``t`` 与子表 ``s`` 都带。
- 时长换算 ``ROUND(EXTRACT(EPOCH FROM (end - start)) * 1000)::int``；PG 的 ``FILTER``
  只能挂聚合本体，故写成 ``ROUND(AVG(...) FILTER (WHERE ...) * 1000)::int``（把
  ``FILTER`` 挂在 ``ROUND`` 外是非法 SQL）。
- **除零**：计数类空集返 0，比率/均值类返 ``null``（``/ NULLIF(分母, 0)`` / ``AVG`` 天然 NULL）。
- **成功率**：分子为 ``count(*)``（``bigint``），必须 ``(count(*) FILTER (...))::numeric``
  再除，否则 PG 走整数除法向零截断（3 成 1 败会返回 0 而非 0.75）。分母保持
  ``NULLIF(..., 0)`` ⇒ 空集仍返 ``NULL``。
- 任务级终态 = ``completed`` / ``failed``；子任务级终态 = ``success`` / ``failed`` / ``timeout``。
- **三族趋势日切口径**：C5 / K2 / T2 **统一按 Asia/Shanghai 自然时/日切桶**
  （固定口径）。C5 / K2 对 ``timestamptz`` 显式 ``timezone('Asia/Shanghai', ...)``
  再 ``date_trunc``；T2 的列本就是北京墙钟 naive，直接 ``date_trunc`` 即等价。
  ⛔ 不读平台设置里的 timezone（仅存不用），也不依赖 DB session 时区。
  ⚠️ **仅分桶边界统一**，输出仍按各族既有 ISO 口径：C5 / K2 回 UTC ``Z``，T2 回 ``+08:00``。
- ``waiting_now`` 为**实时快照**（``status='pending'``，不套区间），不进任何分子/分母。
"""

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.repository.base import BaseRepository

#: ``stage_code`` 白名单：子任务 step_code 只认下列值，未知值不返回。
_STEP_CODES = ("parse", "extract", "compile", "detect", "aggregate")
_STEP_IN = "(" + ", ".join(f"'{c}'" for c in _STEP_CODES) + ")"


class MonitorTaskRepository(BaseRepository):
    """任务监控取数（T1–T4），全部裸 SQL、不 import task_center。"""

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _type_filter(task_type: Optional[str]) -> Tuple[str, dict]:
        """按 ``task_type`` 过滤 —— 它就是**原始 ``task_catalog_code``**，等值匹配。

        ``task_type`` 不做归一：M1 的筛选项直接给出原始 ``task_catalog_code``，
        T2/T3/T4 必须用同一取值等值匹配。曾把 4 个内置码折叠成同一个档位，
        导致前端拿 M1 给的码来筛这里，恒为空集。
        """
        if task_type is None:
            return "", {}
        return " AND t.task_catalog_code = :task_type", {"task_type": task_type}

    # ------------------------------------------------------------------
    # T1 · 统计卡（1 段）
    # ------------------------------------------------------------------

    async def summary(self, start: datetime, end: datetime) -> dict:
        """T1 统计卡：区间内计数 / 比率 / 均值 / 缺失数 + 实时等待快照（同一 SQL 段）。"""
        # MAX(created_at) AS as_of：北京 naive，输出由 service 补 +08:00
        sql = sa_text(
            f"""
            SELECT
                count(*)                                                         AS total_tasks,
                (count(*) FILTER (WHERE status = 'completed'))::numeric
                  / NULLIF(count(*) FILTER (WHERE status IN ('completed', 'failed')), 0)
                                                                                 AS success_rate,
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (finished_at - started_at)))
                    FILTER (
                        WHERE status IN ('completed', 'failed')
                          AND started_at IS NOT NULL AND finished_at IS NOT NULL
                    ) * 1000
                )::int                                                           AS avg_duration_ms,
                count(*) FILTER (
                    WHERE status IN ('completed', 'failed')
                      AND (started_at IS NULL OR finished_at IS NULL)
                )                                                                AS incomplete_count,
                MAX(created_at)                                                  AS as_of,
                (
                    SELECT count(*) FROM task_center.tasks
                    WHERE status = 'pending' AND is_deleted = 0
                )                                                                AS waiting_now
            FROM task_center.tasks
            WHERE created_at >= :start AND created_at < :end
              AND is_deleted = 0
            """
        )
        row = (await self.session.execute(sql, {"start": start, "end": end})).mappings().one()
        return dict(row)

    # ------------------------------------------------------------------
    # T2 · 趋势（1 段）
    # ------------------------------------------------------------------

    async def trend(
        self, start: datetime, end: datetime, bucket: str, task_type: Optional[str]
    ) -> List[dict]:
        """T2 趋势分桶（``bucket`` 白名单 ``hour`` / ``day``，由 service 校验）。"""
        type_sql, type_params = self._type_filter(task_type)
        # created_at 是北京 naive ⇒ date_trunc 直接得到北京自然桶，勿加 AT TIME ZONE
        sql = sa_text(
            f"""
            SELECT
                date_trunc('{bucket}', t.created_at)                             AS bucket_start,
                count(*)                                                         AS task_count,
                (count(*) FILTER (WHERE t.status = 'completed'))::numeric
                  / NULLIF(count(*) FILTER (WHERE t.status IN ('completed', 'failed')), 0)
                                                                                 AS success_rate,
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (t.finished_at - t.started_at)))
                    FILTER (
                        WHERE t.status IN ('completed', 'failed')
                          AND t.started_at IS NOT NULL AND t.finished_at IS NOT NULL
                    ) * 1000
                )::int                                                           AS avg_duration_ms,
                count(*) FILTER (WHERE t.status = 'failed')                      AS failed_count
            FROM task_center.tasks t
            WHERE t.created_at >= :start AND t.created_at < :end
              AND t.is_deleted = 0{type_sql}
            GROUP BY 1 ORDER BY 1
            """
        )
        params = {"start": start, "end": end, **type_params}
        rows = (await self.session.execute(sql, params)).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # T3 · 失败任务列表 + count（2 段）
    # ------------------------------------------------------------------

    async def failures(
        self,
        start: datetime,
        end: datetime,
        task_type: Optional[str],
        offset: int,
        limit: int,
    ) -> Tuple[List[dict], int]:
        """T3 失败任务（``status='failed'``），``ORDER BY finished_at DESC NULLS LAST``。"""
        type_sql, type_params = self._type_filter(task_type)
        base_where = (
            "t.status = 'failed' AND t.created_at >= :start AND t.created_at < :end "
            f"AND t.is_deleted = 0{type_sql}"
        )
        count_sql = sa_text(f"SELECT count(*) AS total FROM task_center.tasks t WHERE {base_where}")
        # t.finished_at：北京 naive，输出由 service 补 +08:00
        list_sql = sa_text(
            f"""
            SELECT
                t.id                 AS task_id,
                t.task_name          AS task_name,
                t.task_catalog_code  AS task_catalog_code,
                t.status             AS status,
                t.error_message      AS error_message,
                t.error_code         AS error_code,
                t.finished_at        AS finished_at
            FROM task_center.tasks t
            WHERE {base_where}
            ORDER BY t.finished_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
            """
        )
        params = {"start": start, "end": end, **type_params}
        total = int((await self.session.execute(count_sql, params)).scalar() or 0)
        rows = (
            await self.session.execute(list_sql, {**params, "limit": limit, "offset": offset})
        ).mappings().all()
        return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # T4 · 运行概览：父表 + 子表（2 段）
    # ------------------------------------------------------------------

    async def overview_parent(
        self, start: datetime, end: datetime, task_type: Optional[str]
    ) -> List[dict]:
        """T4 父表：按**原始 ``task_catalog_code``**（别名 ``task_type``）分组。"""
        type_sql, type_params = self._type_filter(task_type)
        sql = sa_text(
            f"""
            SELECT
                t.task_catalog_code                                              AS task_type,
                count(*)                                                         AS total_count,
                count(*) FILTER (WHERE t.status = 'running')                     AS running_count,
                count(*) FILTER (WHERE t.status = 'pending')                     AS waiting_count,
                count(*) FILTER (WHERE t.status = 'failed')                      AS failed_count,
                (count(*) FILTER (WHERE t.status = 'completed'))::numeric
                  / NULLIF(count(*) FILTER (WHERE t.status IN ('completed', 'failed')), 0)
                                                                                 AS success_rate,
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (t.finished_at - t.started_at)))
                    FILTER (
                        WHERE t.status IN ('completed', 'failed')
                          AND t.started_at IS NOT NULL AND t.finished_at IS NOT NULL
                    ) * 1000
                )::int                                                           AS avg_duration_ms
            FROM task_center.tasks t
            WHERE t.created_at >= :start AND t.created_at < :end
              AND t.is_deleted = 0{type_sql}
            GROUP BY 1
            """
        )
        params = {"start": start, "end": end, **type_params}
        rows = (await self.session.execute(sql, params)).mappings().all()
        return [dict(r) for r in rows]

    async def overview_stages(
        self, start: datetime, end: datetime, task_type: Optional[str]
    ) -> List[dict]:
        """T4 子表：按 (原始 ``task_catalog_code``, step_code) 分组；``step_code`` 走白名单。"""
        type_sql, type_params = self._type_filter(task_type)
        sql = sa_text(
            f"""
            SELECT
                t.task_catalog_code                                              AS task_type,
                s.step_code                                                      AS stage_code,
                MAX(s.subtask_name)                                              AS stage_name,
                count(*)                                                         AS exec_count,
                count(*) FILTER (WHERE s.status = 'running')                     AS running_count,
                count(*) FILTER (WHERE s.status = 'pending')                     AS waiting_count,
                count(*) FILTER (WHERE s.status IN ('failed', 'timeout'))        AS failed_count,
                (count(*) FILTER (WHERE s.status = 'success'))::numeric
                  / NULLIF(
                        count(*) FILTER (WHERE s.status IN ('success', 'failed', 'timeout')), 0
                    )                                                            AS success_rate,
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (s.finished_at - s.started_at)))
                    FILTER (
                        WHERE s.status IN ('success', 'failed', 'timeout')
                          AND s.started_at IS NOT NULL AND s.finished_at IS NOT NULL
                    ) * 1000
                )::int                                                           AS avg_duration_ms
            FROM task_center.subtasks s
            JOIN task_center.tasks t ON t.id = s.task_id
            WHERE t.created_at >= :start AND t.created_at < :end
              AND t.is_deleted = 0
              AND s.is_deleted = 0
              AND s.step_code IN {_STEP_IN}{type_sql}
            GROUP BY 1, 2
            ORDER BY 2
            """
        )
        params = {"start": start, "end": end, **type_params}
        rows = (await self.session.execute(sql, params)).mappings().all()
        return [dict(r) for r in rows]


__all__ = ["MonitorTaskRepository"]
