#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""落点 / 构造的**后台 run**：建单、轮询读取、执行与收口。

为什么单列一个服务而不是塞进 `AbstractSchemaService`：

- `AbstractSchemaService.landing_pass` / `construct` 是**同步语义**的长任务（被
  请求线程直接 await）；后台 run 需要一条可持久化的状态机（queued → running →
  succeeded|failed）与轮询面，两者生命周期不同。
- 后台任务是**唯一**不经过用户请求上下文的写路径：所有写都带租户谓词（仓储层
  已钉死），本层只负责「谁的 run、跑到哪一步、终态是什么」。

并发口径：同一 (租户, 知识库) 至多一个占用中的 run，由部分唯一索引
`uq_abstract_landing_runs_active` 兜底；本层的 `start` 只做两件应用层的事 ——
① 把**过期**的占用行收口成 failed（进程崩溃的遗留），② 撞唯一索引时翻译成 429。
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
from typing import Optional

from sqlalchemy.exc import IntegrityError

from jonex_core.common.config import config
from jonex_core.common.exceptions import (
    InvalidParameterError,
    RateLimitExceededError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ...models.abstract_landing_run import (
    ACTIVE_RUN_STATUSES,
    RUN_MODE_FULL,
    RUN_MODE_LANDINGS,
    RUN_MODES,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_SUCCEEDED,
)
from ...repository.abstract_landing_run_repository import (
    AbstractLandingRunRepository,
)

logger = logging.getLogger(__name__)

#: 占用行多久没心跳就算「进程已死」。默认 30 分钟：正常 run 的进度回调是
#: 逐概念写库（秒级），30 分钟没有任何更新只可能是任务没了。
#: 允许 `config` 覆盖是留给运维的旋钮；配置项尚未进 Settings 时用本默认值，
#: 所以读的是 `getattr` 而不是直接属性（缺键不能让 start 500）。
LANDING_RUN_STALE_SECONDS = 1800

#: 终态错误文本落库上限（与 `error` 列的排障口径一致，避免把整段堆栈塞进库）。
_ERROR_MAX_LEN = 1000


def _stale_seconds() -> float:
    raw = getattr(config, "LANDING_RUN_STALE_SECONDS", LANDING_RUN_STALE_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(LANDING_RUN_STALE_SECONDS)
    # <= 0 = 关闭回收（与保留策略的「0 = 不自动」同口径），不能变成「全判过期」。
    return value if value > 0 else 0.0


def _as_utc(value: dt.datetime) -> dt.datetime:
    """把可能不带时区的时间**按 UTC** 归一。

    库里是 timestamptz（asyncpg 读出来带 UTC），但 ORM 的 `TimestampMixin` 用
    naive `datetime.now` 做默认值，新建未重读的实例可能没有 tzinfo。两边的编码
    口径都是「naive 即 UTC」，这里保持同一假设，只做比较前的统一。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _is_stale(row, *, now: Optional[dt.datetime] = None,
              stale_seconds: Optional[float] = None) -> bool:
    """占用行是否已过期。`updated_at` 缺失时不判过期（宁可挡住也不误杀在跑的 run）。"""
    updated_at = getattr(row, "updated_at", None)
    if updated_at is None:
        return False
    seconds = _stale_seconds() if stale_seconds is None else stale_seconds
    if seconds <= 0:
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    return (current - _as_utc(updated_at)).total_seconds() > seconds


class LandingRunService:
    """一次落点 / 构造运行的状态机与执行入口。

    构造参数：

    - `session`：写路径的数据库会话（`execute` 的后台任务整段持有它）；
    - `repo` / `schema_service`：仅供单测注入 —— 生产路径不传，分别用
      真仓储与 `AbstractSchemaService()`。
    """

    def __init__(self, session, *, repo=None, schema_service=None):
        self._session = session
        self._repo = repo
        self._schema_service = schema_service

    # ------------------------------------------------------------------
    # 依赖（惰性）
    # ------------------------------------------------------------------

    @property
    def repo(self) -> AbstractLandingRunRepository:
        if self._repo is None:
            self._repo = AbstractLandingRunRepository(self._session)
        return self._repo

    def _schema(self):
        if self._schema_service is None:
            # 延迟 import：落点栈会拉起 NL→SQL 检索/模型模块，只做轮询读取的
            # 进程不该为它付 import 成本（与 `_run_landing_agent` 同口径）。
            from ..abstract_schema_service import AbstractSchemaService

            self._schema_service = AbstractSchemaService()
        return self._schema_service

    async def _commit(self) -> None:
        """提交当前会话。

        run 的写路径（set_running / update_progress / finish）都必须**立即提交**：
        轮询 `get` 走另一条会话，不提交就永远读不到 running/进度；而且整段 run
        持行锁会把该 KB 的所有写挡在后面。会话缺失只在单测里出现（注入假 repo）。
        """
        if self._session is not None:
            await self._session.commit()

    # ------------------------------------------------------------------
    # 建单
    # ------------------------------------------------------------------

    async def start(self, tenant_id: str, kb_id: str, user_id: str,
                    run_mode: str, *, force: bool = False,
                    alias_hints: str = "") -> dict:
        """建一条 queued run，返回 `{"run_id", "status"}`。

        冲突口径与 `construct` 一致（429）：同 KB 同时只许一个在飞。**但先做
        stale 回收** —— 进程崩溃会留下一条 `running` 行，部分唯一索引会把它变成
        该 KB 的永久挡板；`updated_at` 超过 `LANDING_RUN_STALE_SECONDS` 就说明
        没有活任务在推进它，收口成 failed 后继续建新单。

        `force` / `alias_hints` 会落到 run 行上供后台 `execute` 使用（见仓储
        `create` 的说明）。**已确认（active）的抽象 schema 在 `force=false` 时
        直接回 1003**：与 `construct` 同口径，前端据此弹二次确认；不这样做的话
        run 会先建单、跑到后台才因 1003 失败，用户看到的是「受理成功→失败」。
        """
        tenant_id = require_tenant(tenant_id)
        if not str(kb_id or "").strip():
            raise InvalidParameterError(message="缺少必填参数：knowledge_base_id")
        if run_mode not in RUN_MODES:
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.invalid_run_mode",
                fallback="run_mode 只能是 full 或 landings",
            ))
        # 开关必须在**建单前**判：若只靠后台 `landing_pass` 里的守卫，start 已经
        # 成功返回 run_id，前端只会看到轮询终态的原始错误，永远走不到 1001 回退。
        if not bool(getattr(config, "ABSTRACT_LANDING_AGENT_ENABLED", False)):
            raise InvalidParameterError(message=translate(
                "err.abstract_schema.landing_agent_disabled",
                fallback="AI 落点未启用（ABSTRACT_LANDING_AGENT_ENABLED=false）",
            ))

        # 已确认的 schema 是只读基线：两种模式都不允许在未显式确认覆盖时动它。
        # 放在任何写之前（包括 stale 回收），被拒的请求不产生任何副作用。
        if not force:
            stored_status = await self._schema().get_status(tenant_id, kb_id)
            if stored_status == "active":
                raise ResourceConflictError(
                    message=translate(
                        "err.abstract_schema.active_exists",
                        fallback="该知识库已有已确认的抽象 schema，重新生成会把它退回"
                                 "未确认状态；确认覆盖请带 force=true 重试",
                    ),
                )

        active = await self.repo.find_active(tenant_id, kb_id)
        if active is not None:
            if _is_stale(active):
                logger.warning(
                    "落点 run 占用行已过期（run=%s kb=%s updated_at=%s）—— "
                    "按 stale 收口后建新单",
                    getattr(active, "id", "?"), kb_id,
                    getattr(active, "updated_at", None),
                )
                await self.repo.finish(
                    tenant_id, active.id, status=RUN_STATUS_FAILED, error="stale",
                )
            else:
                raise RateLimitExceededError(
                    message=translate(
                        "err.abstract_schema.construct_in_flight",
                        fallback="该知识库正在生成/构造中，请稍后再试",
                    ),
                    details={
                        "knowledge_base_id": kb_id,
                        "run_id": getattr(active, "id", None),
                    },
                )

        try:
            row = await self.repo.create(
                tenant_id, kb_id, run_mode, created_by=user_id or "",
                force=bool(force), alias_hints=alias_hints or "",
            )
        except IntegrityError as exc:
            # find_active 与 create 之间的竞态：另一个副本刚建了单。唯一索引是
            # 最终判据，翻译成与上面同口径的 429（不把 500 丢给前端）。
            raise RateLimitExceededError(
                message=translate(
                    "err.abstract_schema.construct_in_flight",
                    fallback="该知识库正在生成/构造中，请稍后再试",
                ),
                details={"knowledge_base_id": kb_id},
            ) from exc

        return {
            "run_id": row.id,
            "status": row.status or RUN_STATUS_QUEUED,
        }

    # ------------------------------------------------------------------
    # 轮询读取
    # ------------------------------------------------------------------

    async def get(self, tenant_id: str, run_id: str,
                  *, kb_id: Optional[str] = None) -> dict:
        """返回轮询形状；**不带 schema 正文**（前端只靠 progress/summary 渲染）。

        `kb_id` 是可选的**归属校验**：能力动作的空间闸门按调用方声明的 KB 判权，
        若不对上「run 真正所属的 KB」，拿 A 库权限 + B 库 run_id 就能读到 B 的
        运行摘要。给定时不一致按「不存在」处理（1002），不泄漏存在性。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self.repo.get(tenant_id, run_id)
        if row is None or (
            kb_id is not None and getattr(row, "knowledge_base_id", None) != kb_id
        ):
            raise ResourceNotFoundError(message=translate(
                "err.common.resource_not_found",
                params={"entity": "landing run", "id": run_id},
                fallback="落点运行不存在",
            ))

        summary = (
            copy.deepcopy(row.summary) if isinstance(row.summary, dict) else {}
        )
        # 报告口径：轮询响应不许带 schema 正文。construct 的摘要里带一份完整
        # schema；落库前已剪掉（见 `execute`），这里再兜一次 —— 前端每 2 秒轮询，
        # 带正文等于把几 MB 的 JSON 反复搬。
        summary.pop("schema", None)
        progress = (
            copy.deepcopy(row.progress) if isinstance(row.progress, dict) else {}
        )
        out = {
            "run_id": row.id,
            "status": row.status,
            "phase": row.phase or "",
            "progress": progress,
            "summary": summary,
            "error": row.error,
        }
        schema_version = summary.get("schema_version")
        if schema_version is not None:
            out["schema_version"] = schema_version
        return out

    # ------------------------------------------------------------------
    # 后台执行
    # ------------------------------------------------------------------

    async def execute(self, tenant_id: str, run_id: str) -> None:
        """后台任务入口：置 running → 按模式跑 → 写终态。

        **不持锁**：锁与心跳在 `landing_pass` / `construct` 内部（它们才知道
        自己跑多久）。本方法只保证「一旦开跑，行一定落到终态」，包括取消：
        v1 没有 cancelled 状态，任务被取消时按 failed 收口，否则占用行会永远
        挡着该 KB 的下一次 run。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self.repo.get(tenant_id, run_id)
        if row is None:
            logger.warning(
                "落点 run 不存在（tenant=%s run=%s）—— 后台任务放弃", tenant_id, run_id,
            )
            return
        if row.status != RUN_STATUS_QUEUED:
            # 只有 queued 能被本方法推进（与仓储 `set_running` 的 SQL 守卫同口径）。
            # `running` 说明另一条投递已经在跑：这里再跑一次会重复写落点；终态行
            # 更是只能读不能改。两种情况都**不写任何东西**（连 finish 都不调）——
            # 否则重复投递会把在跑/成功的行改写掉。
            logger.warning(
                "落点 run 不是 queued（run=%s status=%s）—— 重复投递或已终态，跳过",
                run_id, row.status,
            )
            return

        # 状态迁移是**唯一**的重复投递判据：另一条投递已经把行改成 running/终态时
        # 本语句 0 行命中，此时必须立刻放弃（一点写入都不做），否则同一批落点会被
        # 重跑、进度/终态被覆盖。提交见 `_commit` 的说明。
        transitioned = await self.repo.set_running(tenant_id, run_id)
        if not transitioned:
            logger.warning(
                "落点 run 状态迁移未命中（run=%s kb=%s）—— 重复投递或已终态，跳过",
                run_id, row.knowledge_base_id,
            )
            return
        await self._commit()
        kb_id = row.knowledge_base_id
        user_id = getattr(row, "created_by", "") or ""
        run_mode = row.run_mode or RUN_MODE_LANDINGS
        # 提交时冻结的构造参数（见 `start`）。调度层只传 run_id，这里以行为准。
        force = bool(getattr(row, "force", False))
        alias_hints = str(getattr(row, "alias_hints", "") or "")

        async def _on_progress(payload) -> None:
            """runner 经 `_progress_cb_adapter` 给的是 dict（不是 dataclass）。"""
            if not isinstance(payload, dict):
                payload = {"phase": str(getattr(payload, "phase", "") or "")}
            await self.repo.update_progress(
                tenant_id, run_id,
                phase=str(payload.get("phase") or ""),
                progress=payload,
            )
            # 逐概念进度是前端轮询的主视图：不提交，另一端会话永远读不到 running。
            await self._commit()

        try:
            if run_mode == RUN_MODE_FULL:
                # 先落一个粗粒度相位兜底：construct 内部会按阶段逐条回写
                # （读取 → 聚类 → 合并 → 落点 → 关系 → 命名 → 写库），每条覆盖它。
                await self.repo.update_progress(
                    tenant_id, run_id,
                    phase="construct", progress={"phase": "construct"},
                )
                await self._commit()
                # 「全部重来」= replace + 显式 force 透传：append 会在旧草稿后面追加，
                # 而 active 草稿会直接 1003 —— 两者都不是用户点「全部重来」的语义。
                result = await self._schema().construct(
                    tenant_id, kb_id,
                    user_id=user_id,
                    seed="document",
                    auto_landings=True,
                    mode="replace",
                    force=force,
                    alias_hints=alias_hints,
                    landing_mode="agent",
                    # 阶段进度（读取 → 聚类 → 合并 → 落点 → 关系 → 命名 → 写库）：
                    # 前端弹层靠它显示「目前在做什么」，构造内部逐阶段回写 run 行。
                    progress_cb=_on_progress,
                )
                # construct 的摘要是 `row.to_dict()` 叠统计字段，里面有一份完整
                # schema。run 的 summary 只留统计（get 的契约不许带正文）。
                summary = {
                    key: value for key, value in (result or {}).items()
                    if key != "schema"
                }
                await self.repo.update_progress(
                    tenant_id, run_id,
                    phase="done", progress={"phase": "done"},
                )
                await self._commit()
            else:
                result = await self._schema().landing_pass(
                    tenant_id, kb_id, user_id=user_id, progress_cb=_on_progress,
                )
                result = result or {}
                summary = {
                    key: result[key]
                    for key in ("landings", "warnings", "schema_version",
                                "coverage", "evidence_gap")
                    if key in result
                }
                if alias_hints:
                    # landings-only 不重造概念/别名（那是 full 的职责），词表在这里
                    # 只会被静默丢弃 —— 显式告诉用户它没生效，而不是假装用了。
                    warnings = list(summary.get("warnings") or [])
                    warnings.append("业务词表仅在全部重来时生效")
                    summary["warnings"] = warnings

            await self.repo.finish(
                tenant_id, run_id, status=RUN_STATUS_SUCCEEDED, summary=summary,
            )
            await self._commit()
        except asyncio.CancelledError:
            # 取消也要落到终态：能写就写，写不了至少留日志，然后**继续传播取消**
            # （吞掉 CancelledError 会让关闭流程挂住）。
            try:
                await self.repo.finish(
                    tenant_id, run_id, status=RUN_STATUS_FAILED, error="cancelled",
                )
                await self._commit()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不能盖掉取消语义
                logger.warning(
                    "落点 run 取消时的终态写入失败 run=%s: %s", run_id, exc,
                )
            raise
        except Exception as exc:  # noqa: BLE001 —— 业务失败收口成 failed，不上抛
            # 被调方的语义是「失败不落库」；这里只负责把 run 行收口，异常文本
            # 截断后落库供前端/排障读取。
            error = (str(exc) or repr(exc))[:_ERROR_MAX_LEN]
            logger.warning("落点 run 执行失败 run=%s: %s", run_id, error)
            await self.repo.finish(
                tenant_id, run_id, status=RUN_STATUS_FAILED, error=error,
            )
            await self._commit()

    async def fail_if_active(self, tenant_id: str, run_id: str, error: str) -> bool:
        """兜底收口：**只在行仍占用中**时写 failed，返回是否真的改了。

        调度层的最后一道防线（造会话 / 收口语句本身抛错之后的补偿）会用它。
        不加状态守卫就会把已经 `succeeded` 的 run 盖成 failed —— 摘要与终态
        一起丢，且前端看到的是假失败。
        """
        tenant_id = require_tenant(tenant_id)
        row = await self.repo.get(tenant_id, run_id)
        if row is None or row.status not in ACTIVE_RUN_STATUSES:
            return False
        await self.repo.finish(
            tenant_id, run_id,
            status=RUN_STATUS_FAILED,
            error=(str(error or "background error"))[:_ERROR_MAX_LEN],
        )
        return True


__all__ = [
    "LANDING_RUN_STALE_SECONDS",
    "LandingRunService",
]
