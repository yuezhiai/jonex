# -*- coding:utf-8 -*-
"""演化台服务化 ②b：失败诊断（观测窗 → 归因报告 → DB）。

从已退役的本地桥接（宿主 CLI 版，代码已删除）的 `/failures`、`/diagnosis`、
`/diagnosis/latest` 三端点平移，两条口径原样保留：

- **失败行**：`nl2sql_observations` 里 `outcome='failed'` 且 `reason_code` 非空、
  `created_at` 半开窗口 `[since, until)`、按时间倒序、`LIMIT 5000`（与 CLI
  `tests/eval/nl2sql/diagnose.py` 的 `_SQL` 同窗口口径；**多一道 tenant 过滤**，
  桥接版没有租户列）；
- **归因**：直接调 `services/nl2sql/diagnosis.diagnose`（纯函数），规则表 / 提案 id /
  reject 过滤只有那一份实现 —— 本模块不复制任何归因逻辑。

载体从文件换成表：报告落 `knowledge_base.evolution_diagnosis_reports`（迁移 017），
`json_path` 是 `db://evolution_diagnosis_reports/{id}`；没有 Markdown 载体，`md_path`
恒 `None`（不伪造）。
"""
from __future__ import annotations

import datetime
import json
import uuid

from sqlalchemy import func, select

from ...models.evolution import EvolutionLedger
from ...models.evolution_diagnosis import EvolutionDiagnosisReport
from ...models.nl2sql_observation import Nl2SqlObservation
from ..nl2sql.diagnosis import diagnose

#: 失败行窗口上限（与 CLI `_SQL` 的 LIMIT 同值）：诊断是抽样归因，不是全量导出。
MAX_FAILURES = 5000

_JSON_PATH = "db://evolution_diagnosis_reports/{id}"


def _iso(value):
    """slim 时间口径：datetime → ISO 字符串（桥接 `str(...)` 的可序列化等价物）。

    `None` 原样返回；非 datetime（异常驱动 / 老口径的 str）`str()` 兜底 —— 观测是旁路
    数据，一条坏行不该让整个列表裸崩。
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _scene_of(row) -> dict:
    """`scene` 归一成 dict（与 CLI `_normalise` 同口径）；坏 JSON / 非 dict → `{}`。"""
    scene = getattr(row, "scene", None)
    if isinstance(scene, str):
        try:
            scene = json.loads(scene)
        except json.JSONDecodeError:
            return {}
    return scene if isinstance(scene, dict) else {}


def _slim(row) -> dict:
    """桥接 `/failures` 的 slim 七键（前端 `FailureRow` 契约）。"""
    return {
        "created_at": _iso(getattr(row, "created_at", None)),
        "question": getattr(row, "question", None),
        "reason_code": getattr(row, "reason_code", None),
        "reason": getattr(row, "reason", None),
        "sql": getattr(row, "sql", None),
        "rejected_sql": getattr(row, "rejected_sql", None),
        "resource_id": getattr(row, "data_source_id", None),
    }


def _entry(row) -> dict:
    """观测行 → `diagnose()` 条目（与 CLI `_load_entries` 的列集同构 + tenant/outcome）。

    `diagnose` 的归因要 `reason/scene/question`、分组要 `resource_id`，窗口过滤要
    `created_at`；`tenant_id`/`outcome` 是「这一行属于谁、是什么状态」的留痕，
    `diagnose` 本身不据此过滤（entries 已在 SQL 侧按租户筛过）。
    """
    return {
        "tenant_id": getattr(row, "tenant_id", None),
        "outcome": getattr(row, "outcome", None),
        "created_at": getattr(row, "created_at", None),
        "scene": _scene_of(row),
        "reason_code": getattr(row, "reason_code", None),
        "reason": getattr(row, "reason", None),
        "sql": getattr(row, "sql", None),
        "rejected_sql": getattr(row, "rejected_sql", None),
        "resource_id": getattr(row, "data_source_id", None),
        "question": getattr(row, "question", None),
    }


async def _load_failed_rows(session, tenant_id: str, since: datetime.datetime,
                            until: datetime.datetime) -> list:
    """失败观测行（ORM 行对象）：半开窗口 `[since, until)`、按时间倒序、上限 5000。"""
    stmt = (
        select(Nl2SqlObservation)
        .where(
            Nl2SqlObservation.tenant_id == tenant_id,
            Nl2SqlObservation.outcome == "failed",
            Nl2SqlObservation.reason_code.is_not(None),
            Nl2SqlObservation.reason_code != "",
            Nl2SqlObservation.created_at >= since,
            Nl2SqlObservation.created_at < until,
            # 演化基准（合成流量）不入失败诊断（Task 4）：scene 标记整类排除。
            # 缺键 = NULL → coalesce 成 'false'（普通观测照常进）；坏 scene 同理。
            func.coalesce(
                Nl2SqlObservation.scene["evolution_bench"].astext, "false"
            ) != "true",
        )
        .order_by(Nl2SqlObservation.created_at.desc())
        .limit(MAX_FAILURES)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_failures(session, tenant_id: str, since: datetime.datetime,
                        until: datetime.datetime) -> list[dict]:
    """窗口内的失败观测（slim 七键，桥接 `/failures` 同形）。"""
    rows = await _load_failed_rows(session, tenant_id, since, until)
    return [_slim(row) for row in rows]


async def _reject_ids(session, tenant_id: str) -> set[str]:
    """该租户 reject 台账里的提案 id（`entry.id`）；坏条目 / 缺 id 静默跳过。

    reject 台账的形状是 `{"id","at","note"}`（`AdoptService.reject` 钉死）；
    老数据/手改坏的行不该让整次诊断裸崩 —— 少一条否决最多让提案重新冒出来，
    而裸崩会让整个诊断不可用。

    `builders.load_reject_ids` 是同口径的另一份，但签名不收 session（自己开
    `get_db_session()`）；`run_diagnosis` 用外部注入的会话（报告写库与本次读台账
    同会话，测试也靠会话注入），故不复用、保留本份。
    """
    rows = (await session.execute(
        select(EvolutionLedger.entry).where(
            EvolutionLedger.tenant_id == tenant_id,
            EvolutionLedger.ledger_type == "reject",
        ))).scalars().all()
    ids: set[str] = set()
    for entry in rows:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


async def run_diagnosis(session, tenant_id: str, since: datetime.datetime,
                        until: datetime.datetime,
                        reject_ids: set[str] | None = None,
                        created_by: str | None = None) -> dict:
    """读窗口内失败行 → `diagnose()` → 报告落库。

    `reject_ids` 缺省（None）从 reject 台账取；显式传入（含空集合）就不再读台账 ——
    「显式声明没有否决」与「没给、去查」是两件事。

    `created_by` 是操作人 `user_id`（能力动作传 `request.user_id`）；拿不到身份就
    留 None —— 列可空，「无人操作」（系统调用 / 网关未带身份）是合法状态。

    返回顶层带 `since` / `until`（ISO 字符串，分钟精度，桥接 `/diagnosis` 信封同形）；
    精确窗口仍以行内 `params` 为准（完整 `isoformat()`，不截断）。
    """
    rows = await _load_failed_rows(session, tenant_id, since, until)
    entries = [_entry(row) for row in rows]
    if reject_ids is None:
        reject_ids = await _reject_ids(session, tenant_id)

    report = diagnose(entries, now=until, since=since, until=until,
                      reject_ids=reject_ids)

    row = EvolutionDiagnosisReport(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        params={"since": since.isoformat(), "until": until.isoformat()},
        report=report,
        created_by=created_by,
    )
    session.add(row)
    try:
        await session.commit()
    except Exception:
        # commit 失败后会话仍处于失败事务里：不回滚，后续任何语句都会连锁报错
        # （与 `LedgerService.append` 同口径）。
        await session.rollback()
        raise

    return {"report": report,
            "since": since.isoformat(timespec="minutes"),
            "until": until.isoformat(timespec="minutes"),
            "json_path": _JSON_PATH.format(id=row.id),
            "md_path": None}


async def latest_report(session, tenant_id: str) -> dict:
    """同租户最新一份诊断报告；没有 → null 形状（不伪造空报告）。"""
    stmt = (
        select(EvolutionDiagnosisReport)
        .where(EvolutionDiagnosisReport.tenant_id == tenant_id)
        .order_by(EvolutionDiagnosisReport.created_at.desc(),
                  EvolutionDiagnosisReport.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return {"report": None, "json_path": None, "md_path": None}
    return {"report": row.report, "json_path": _JSON_PATH.format(id=row.id),
            "md_path": None}
