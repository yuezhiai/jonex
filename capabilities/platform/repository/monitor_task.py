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
- T4 阶段行 ``waiting_count`` 只算**父任务仍未结束**（``pending`` / ``running``）的
  pending 子任务：父任务已终态时剩下的 pending 步骤永远不会再执行，属于「未执行」
  而非「在排队」。不判父任务会与任务中心「处理中」对不上（2026-10-26 实测：解析
  失败任务的下游 ``compile`` 子任务永远 ``pending``）。
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
        """按 ``task_type`` 过滤 —— 2026-10-23 起它就是**编排定义 ID**，等值匹配。

        以前 ``task_type`` 是 ``task_catalog_code``（内置任务目录码）。目录这一层
        删除后，监控的「任务类型」改由编排定义承载：``tasks.definition_id`` 是建任务
        时的快照，定义被删了也还在，所以历史任务照样筛得到。

        ``task_type`` 不做归一：M1 的筛选项直接给出原始 ``definition_id``，
        T2/T3/T4 必须用同一取值等值匹配。
        """
        if task_type is None:
            return "", {}
        return " AND t.definition_id = :task_type", {"task_type": task_type}

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
        """T2 趋势分桶（``bucket`` 白名单 ``5m`` / ``hour`` / ``day``，由 service 校验）。

        ``hour`` / ``day`` 走 ``date_trunc``；``5m`` 走 ``date_bin``（对齐 5 分钟
        网格，起点固定为 1970-01-01，naive 北京墙钟下与墙钟截断等价）。
        """
        type_sql, type_params = self._type_filter(task_type)
        # created_at 是北京 naive ⇒ date_trunc 直接得到北京自然桶，勿加 AT TIME ZONE
        if bucket == "5m":
            bucket_expr = "date_bin(INTERVAL '5 minutes', t.created_at, TIMESTAMP '1970-01-01')"
        else:
            bucket_expr = f"date_trunc('{bucket}', t.created_at)"
        sql = sa_text(
            f"""
            SELECT
                {bucket_expr}                                                    AS bucket_start,
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
                t.definition_id      AS task_type,
                t.status             AS status,
                t.error_message      AS error_message,
                t.error_code         AS error_code,
                -- 聚合外壳（aggregate_failed）的失败节点在 error_params.parts 里，
                -- T3 的 reason 归并按零件判阶段，少了它只能按外壳猜（2026-10-23 修）
                t.error_params       AS error_params,
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
        """T4 父表：按**编排定义 ID**（``tasks.definition_id``，别名 ``task_type``）分组。"""
        type_sql, type_params = self._type_filter(task_type)
        sql = sa_text(
            f"""
            SELECT
                t.definition_id                                                  AS task_type,
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
        """T4 子表：按 (``tasks.definition_id``, step_code) 分组；``step_code`` 走白名单。"""
        type_sql, type_params = self._type_filter(task_type)
        # 任务级重跑会**新插一套 subtasks**、旧 attempt 的行不软删 ⇒ 只按 task_id 关联会把
        # 历次重跑的步骤一起算进计数/成功率/耗时。必须与 ``tasks.current_attempt`` 对齐，
        # 才与任务中心 ``list_current_by_task`` 同口径；孤儿 subtask 由 INNER JOIN 静默排除。
        sql = sa_text(
            f"""
            SELECT
                t.definition_id                                                  AS task_type,
                s.step_code                                                      AS stage_code,
                MAX(s.subtask_name)                                              AS stage_name,
                count(*)                                                         AS exec_count,
                count(*) FILTER (WHERE s.status = 'running')                     AS running_count,
                -- 「等待中」只算父任务**还没结束**的 pending 步骤。父任务已终态
                -- （completed/failed/stopped）时，剩下的 pending 步骤永远不会再执行
                -- ——那是「未执行」，不是「在排队」。不判父任务会把这类僵尸行算进
                -- 等待：2026-10-26 实测，解析失败任务的下游 compile 子任务永远 pending，
                -- 概览显示「知识编译 · 等待中 1」，而任务中心「处理中」为空。
                count(*) FILTER (
                    WHERE s.status = 'pending'
                      AND t.status IN ('pending', 'running')
                )                                                                AS waiting_count,
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
            JOIN task_center.task_attempts a
              ON a.id = s.task_attempt_id
             AND a.task_id = t.id
             AND a.attempt = t.current_attempt
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
