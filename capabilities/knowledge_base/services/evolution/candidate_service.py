# -*- coding:utf-8 -*-
"""演化候选存储（迁移 024 / 演化台信任闭环 P0）。

`knowledge_base.evolution_candidates` **自身没有 `tenant_id`**：租户隔离一律经
父表 `evolution_runs` 承担 ——
- `persist` 先校验该 run 属于该租户（不属于与不存在同口径 → 1002）；
- `list_for_run` 用 `JOIN evolution_runs r ON r.id = c.run_id
  AND r.tenant_id = :tenant_id` 过滤。

本模块只做存储原语，不生成候选（生成在 `builders.build_candidate_record`，
纯函数）也不做门禁（门禁在 candidate-ran 之后由后续任务回填）。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import uuid

from sqlalchemy import text

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import ResourceNotFoundError

_TABLE = "knowledge_base.evolution_candidates"

#: 读/写共用的列序（`created_at` 由数据库生成，INSERT 不写）。
_COLUMNS = (
    "id", "run_id", "candidate_no", "parent_version_id", "layer",
    "edit_type", "target", "before", "after", "expected_changes",
    "rationale", "evidence", "gate_decision", "gate_reasons", "status",
    "created_at",
)

#: JSONB 列：写入时走 CAST ... AS jsonb（asyncpg 的 jsonb codec 收字符串）。
_JSON_COLUMNS = ("target", "before", "after", "expected_changes", "evidence",
                 "gate_reasons")

#: 迁移 024 `ck_evo_candidate_status` 的白名单。
_STATUSES = frozenset({"draft", "evaluated", "adopted", "rejected",
                       "superseded"})

_RUN_OWNER_SQL = text("""
    SELECT id FROM knowledge_base.evolution_runs
    WHERE id = :run_id AND tenant_id = :tenant_id
    LIMIT 1
""")

_INSERT_SQL = text("""
    INSERT INTO knowledge_base.evolution_candidates
        (id, run_id, candidate_no, parent_version_id, layer, edit_type,
         target, before, after, expected_changes, rationale, evidence,
         gate_decision, gate_reasons, status)
    VALUES
        (:id, :run_id, :candidate_no, :parent_version_id, :layer, :edit_type,
         CAST(:target AS jsonb), CAST(:before AS jsonb), CAST(:after AS jsonb),
         CAST(:expected_changes AS jsonb), :rationale,
         CAST(:evidence AS jsonb), :gate_decision,
         CAST(:gate_reasons AS jsonb), :status)
    RETURNING created_at
""")

_LIST_SQL = text("""
    SELECT c.id, c.run_id, c.candidate_no, c.parent_version_id, c.layer,
           c.edit_type, c.target, c.before, c.after, c.expected_changes,
           c.rationale, c.evidence, c.gate_decision, c.gate_reasons,
           c.status, c.created_at
    FROM knowledge_base.evolution_candidates AS c
    JOIN knowledge_base.evolution_runs AS r
      ON r.id = c.run_id AND r.tenant_id = :tenant_id
    WHERE c.run_id = :run_id
    ORDER BY c.candidate_no, c.id
""")

#: 补 truth 后回写 `evidence.adhoc_cases`（verify_adhoc）。租户隔离由父表
#: JOIN 承担：不是该租户的 run → 命中 0 行，与「没有候选」同口径。
_UPDATE_EVIDENCE_SQL = text("""
    UPDATE knowledge_base.evolution_candidates AS c
    SET evidence = CAST(:evidence AS jsonb)
    FROM knowledge_base.evolution_runs AS r
    WHERE c.run_id = r.id
      AND r.tenant_id = :tenant_id
      AND c.run_id = :run_id
""")

#: 候选生命周期（I6）：租户经父表 JOIN 校验；gate 只在显式给出时覆盖。
_UPDATE_STATUS_SQL = text("""
    UPDATE knowledge_base.evolution_candidates AS c
    SET status = :status,
        gate_decision = COALESCE(CAST(:gate_decision AS varchar),
                                 c.gate_decision),
        gate_reasons = CASE WHEN :gate_reasons IS NULL
                            THEN c.gate_reasons
                            ELSE CAST(:gate_reasons AS jsonb) END
    FROM knowledge_base.evolution_runs AS r
    WHERE c.run_id = r.id
      AND r.tenant_id = :tenant_id
      AND c.run_id = :run_id
      AND c.candidate_no = :candidate_no
""")

#: 同 run 的**后续**候选（P0 只有 candidate_no=1，幂等 no-op）。
_SUPERSEDE_OTHERS_SQL = text("""
    UPDATE knowledge_base.evolution_candidates AS c
    SET status = 'superseded'
    FROM knowledge_base.evolution_runs AS r
    WHERE c.run_id = r.id
      AND r.tenant_id = :tenant_id
      AND c.run_id = :run_id
      AND c.candidate_no > :candidate_no
      AND c.status IN ('draft', 'evaluated')
""")


def _canonical_json(value) -> str:
    """JSONB 参数：规范化 JSON 字符串（键序稳定，中文不转义）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _as_json(value):
    """读取侧容错：asyncpg 正常返回 dict，驱动配置差异下可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _candidate_dict(row) -> dict:
    out = {
        "id": row.id,
        "run_id": row.run_id,
        "candidate_no": int(row.candidate_no),
        "parent_version_id": row.parent_version_id,
        "layer": row.layer,
        "edit_type": row.edit_type,
        "rationale": row.rationale,
        "gate_decision": row.gate_decision,
        "gate_reasons": _as_json(row.gate_reasons),
        "status": row.status,
        "created_at": _iso(row.created_at),
    }
    for name in _JSON_COLUMNS:
        if name in out:
            continue
        out[name] = _as_json(getattr(row, name))
    return out


class CandidateService:
    """`evolution_candidates` 的读写原语。

    `session` 可注入（engine / 测试复用同一会话）；为 None 时自开
    `get_db_session()`。租户校验与读写在同一次会话里完成。
    """

    def __init__(self, session=None) -> None:
        self.session = session

    @contextlib.asynccontextmanager
    async def _scope(self):
        if self.session is not None:
            yield self.session
        else:
            async with get_db_session() as session:
                yield session

    async def persist(self, tenant_id: str, run_id: str, record: dict) -> dict:
        """写入一条 `draft` 候选；run 不属于该租户 → `ResourceNotFoundError`（1002）。

        `candidate_no` 取记录里的值（P0 恒为 1；迁移的 `(run_id, candidate_no)`
        唯一约束兜底）。写入失败先 rollback 再上抛，避免毒化共享会话。
        """
        record = record or {}
        candidate_id = f"cand_{uuid.uuid4().hex}"
        candidate_no = int(record.get("candidate_no") or 1)
        values = {
            "id": candidate_id,
            "run_id": run_id,
            "candidate_no": candidate_no,
            "parent_version_id": record.get("parent_version_id"),
            "layer": record.get("layer") or "content",
            "edit_type": record.get("edit_type") or "",
            "target": _canonical_json(record.get("target") or []),
            "before": _canonical_json(record.get("before") or {}),
            "after": _canonical_json(record.get("after") or {}),
            "expected_changes": _canonical_json(
                record.get("expected_changes") or []),
            "rationale": record.get("rationale"),
            "evidence": _canonical_json(record.get("evidence") or {}),
            "gate_decision": record.get("gate_decision"),
            "gate_reasons": (None if record.get("gate_reasons") is None
                             else _canonical_json(record.get("gate_reasons"))),
            "status": "draft",
        }

        async with self._scope() as session:
            owner = (await session.execute(
                _RUN_OWNER_SQL,
                {"tenant_id": tenant_id, "run_id": run_id})).first()
            if owner is None:
                raise ResourceNotFoundError(message=f"找不到 run: {run_id}")
            try:
                created = (await session.execute(_INSERT_SQL, values)).first()
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        return {
            "id": candidate_id,
            "run_id": run_id,
            "candidate_no": candidate_no,
            "parent_version_id": values["parent_version_id"],
            "layer": values["layer"],
            "edit_type": values["edit_type"],
            "target": _as_json(values["target"]),
            "before": _as_json(values["before"]),
            "after": _as_json(values["after"]),
            "expected_changes": _as_json(values["expected_changes"]),
            "rationale": values["rationale"],
            "evidence": _as_json(values["evidence"]),
            "gate_decision": values["gate_decision"],
            "gate_reasons": (_as_json(values["gate_reasons"])
                             if values["gate_reasons"] is not None else None),
            "status": "draft",
            "created_at": _iso(created[0] if created is not None else None),
        }

    async def list_for_run(self, tenant_id: str, run_id: str) -> list[dict]:
        """该 run 的候选（按 `candidate_no`）；租户过滤由父表 join 承担。"""
        async with self._scope() as session:
            rows = (await session.execute(
                _LIST_SQL,
                {"tenant_id": tenant_id, "run_id": run_id})).all()
        return [_candidate_dict(row) for row in rows]

    async def update_evidence(self, tenant_id: str, run_id: str,
                              evidence: dict) -> int:
        """回写候选的整份 `evidence`（Task 5 补 truth 后的 ad-hoc 结论）。

        返回命中行数；`0` 表示该租户下没有这个 run 的候选（legacy run /
        候选未落库），调用方按旁路处理。失败先 rollback 再上抛。
        """
        values = {"tenant_id": tenant_id, "run_id": run_id,
                  "evidence": _canonical_json(evidence or {})}
        async with self._scope() as session:
            try:
                result = await session.execute(_UPDATE_EVIDENCE_SQL, values)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return int(result.rowcount or 0)

    async def update_status(self, tenant_id: str, run_id: str, status: str,
                            gate: dict | None = None,
                            *, candidate_no: int = 1,
                            supersede_others: bool = False) -> int:
        """推进候选生命周期（engine 门禁 / 采纳 / 否决收口共用）。

        `gate` 给了就一并写 `gate_decision` / `gate_reasons`（取 `decision` /
        `reasons`）；没给则保留原值。`supersede_others=True` 时把同 run 的
        **后续**候选（`candidate_no > candidate_no`）里仍是 draft/evaluated 的
        置 `superseded`（P0 只有 1 号候选，幂等 no-op）。

        租户隔离由父表 JOIN 承担：不是该租户的 run → 命中 0 行。返回主更新
        命中的候选数。失败先 rollback 再上抛（调用方按旁路吞掉）。
        """
        if status not in _STATUSES:
            raise ValueError(f"非法候选状态: {status}")
        gate = gate if isinstance(gate, dict) else {}
        gate_decision = gate.get("decision")
        gate_reasons = gate.get("reasons")
        params = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "status": status,
            "candidate_no": int(candidate_no),
            "gate_decision": (str(gate_decision)
                              if gate_decision is not None else None),
            "gate_reasons": (None if gate_reasons is None
                             else _canonical_json(gate_reasons)),
        }
        async with self._scope() as session:
            try:
                result = await session.execute(_UPDATE_STATUS_SQL, params)
                hit = int(result.rowcount or 0)
                if supersede_others:
                    await session.execute(
                        _SUPERSEDE_OTHERS_SQL,
                        {"tenant_id": tenant_id, "run_id": run_id,
                         "candidate_no": int(candidate_no)})
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return hit


__all__ = ["CandidateService"]
