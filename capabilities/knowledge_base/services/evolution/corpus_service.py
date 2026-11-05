# -*- coding:utf-8 -*-
"""演化语料：DB 读写 + 形态转换（与旧 corpus.yaml 同形）。

除 CRUD 外，本模块还承载「ad-hoc 验证通过后提升为语料」的批量入口
（`CorpusService.promote_from_run`，P1-2 Task 1）。语义要点：

- 去重键是 `adhoc_case_id(question)`（问句归一后的 sha1），不是 evidence 里
  那个可能被手写改过的 `case_id`；
- 同键同内容 → `skipped(duplicate)`；同键不同内容 → `conflicts` +
  `skipped(conflict)`，**绝不覆盖**既有语料行；
- 批量原子：一次 commit 落全部新行；失败整体回滚，不落半批。
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import desc, select, text
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)

from ...models.evolution import EvolutionCorpusCase
from . import audit, corpus_versions
from .run_store import RunStore

logger = logging.getLogger(__name__)

_ALLOWED_EXPECTS = ("answer", "refuse")

#: 提升出来的语料行的 tier（R2）；名字默认 `adhoc_<sha1[:12]>`。
PROMOTE_TIER = "adhoc"
PROMOTE_NAME_PREFIX = "adhoc_"
#: 语料名上限（`EvolutionCorpusCase.name` VARCHAR(128)）。
PROMOTE_NAME_MAX = 128
#: 审计动作名（`platform.audit_logs.action`）。
PROMOTE_AUDIT_ACTION = "evolution.promote_adhoc"


def _validate_expect_control(expect: str, control) -> None:
    """expect 白名单 + answer 必须带非空 control（create 前置、update 落值后各校验一次）。"""
    if expect not in _ALLOWED_EXPECTS:
        raise InvalidParameterError(message="expect 必须是 answer 或 refuse")
    if expect == "answer" and not (isinstance(control, list) and control):
        raise InvalidParameterError(
            message="expect 为 answer 时必须提供非空的 control 列表")


def _case_dict(row) -> dict:
    out = {"name": row.name, "question": row.question,
           "tier": row.tier, "expect": row.expect}
    if row.expect == "answer":
        out["control"] = list(row.control or [])
    return out


# ---------------------------------------------------------------- ad-hoc 提升
def _promotion_draft(case) -> dict | None:
    """verified 且 truth 可用的 ad-hoc case → 语料行草稿；否则 None。

    与 `smoke._plan_sets` 的判定同口径（`answer` 必须有非空 control；
    `refuse` 只要 truth 存在），区别是这里要求 `verification == "verified"`
    —— 未验证的 case 没有可信真值，不能进语料。
    """
    if not isinstance(case, dict) or case.get("verification") != "verified":
        return None
    question = str(case.get("question") or "").strip()
    truth = case.get("truth")
    if not question or not isinstance(truth, dict):
        return None
    expect = str(truth.get("expect") or "")
    if expect == "refuse":
        return {"question": question, "expect": "refuse", "control": None}
    control = truth.get("control")
    if expect != "answer" or not isinstance(control, list):
        return None
    sqls = [str(sql).strip() for sql in control]
    if not sqls or any(not sql for sql in sqls):
        return None
    return {"question": question, "expect": "answer", "control": sqls}


def _needs_sql_truth(case) -> bool:
    """已验真但缺少可提升的 SQL 真值 → 提升时记 `skipped(no_sql_truth)`。

    人审判定（`manual_adhoc_outcomes`）只给结论、不给 SQL（`truth=None`），
    这类 case 不能进语料，但也不该让同批其它 case 的整单请求失败；前端
    默认不勾选它们。`verification=verified` 且 `truth=None` 同口径（可能是
    历史数据/其它人审来源），其余「truth 存在但不可用」仍走原有 1001。
    """
    if not isinstance(case, dict):
        return False
    if case.get("verified_by") == "human":
        return True
    return case.get("verification") == "verified" and case.get("truth") is None


def _normalise_case_ids(value) -> list[str] | None:
    """`case_ids`：None = 全部 verified；给定则必须是非空字符串数组。

    非数组 / 含空串（`""` / 纯空白）→ 1001。空数组同样 1001：静默当成
    「全部」会把用户明确排除的 case 也提上来，恰好是反义。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameterError(message="case_ids 必须是字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidParameterError(
                message="case_ids 必须是非空字符串数组")
        case_id = item.strip()
        if case_id not in seen:
            seen.add(case_id)
            out.append(case_id)
    if not out:
        raise InvalidParameterError(message="case_ids 不能为空")
    return out


def _promotion_name(sha1: str, taken: set[str]) -> str:
    """R2 默认名 `adhoc_<sha1[:12]>`；撞名时延长 sha1 后缀，**绝不覆盖**。

    唯一索引是 `(tenant_id, name)` —— 同租户另一个 KB 提过同一问句时，短后缀
    会撞上。逐步延长到 20 / 40 位，最后才加确定性序号（理论不可达）。
    """
    base = f"{PROMOTE_NAME_PREFIX}{sha1}"
    for width in (12, 20, 40):
        name = f"{PROMOTE_NAME_PREFIX}{sha1[:width]}"
        if name not in taken:
            return name[:PROMOTE_NAME_MAX]
    suffix = 2
    while f"{base}_{suffix}"[:PROMOTE_NAME_MAX] in taken:
        suffix += 1
    return f"{base}_{suffix}"[:PROMOTE_NAME_MAX]


def _candidate_adhoc_cases(candidates) -> list[dict]:
    """展平候选行的 `evidence.adhoc_cases`；同 `case_id` 只留一条。

    **候选表**才是 `verify_adhoc` 的回写目标（`_write_adhoc_evidence` →
    `CandidateService.update_evidence`），`run.patch.candidate.evidence`
    从不被刷新。因此 verification 状态以候选行为准；同一 case_id 出现多次
    （每个候选一条）时保留「verified > 带 truth > 先出现」的那条。
    """
    def rank(case: dict) -> tuple[int, int]:
        return (1 if case.get("verification") == "verified" else 0,
                1 if isinstance(case.get("truth"), dict) else 0)

    out: dict[str, dict] = {}
    for candidate in candidates or []:
        evidence = (candidate or {}).get("evidence") \
            if isinstance(candidate, dict) else None
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except ValueError:
                evidence = None
        cases = evidence.get("adhoc_cases") \
            if isinstance(evidence, dict) else None
        for case in cases or []:
            if not isinstance(case, dict):
                continue
            case_id = str(case.get("case_id") or "").strip()
            if not case_id:
                continue
            existing = out.get(case_id)
            if existing is None or rank(case) > rank(existing):
                out[case_id] = case
    return list(out.values())


def _same_content(row, expect: str, control) -> bool:
    """既有语料行与新 case 的 `expect` + `control` 是否一致（R1）。

    两侧的 SQL 都逐条 `strip` 归一（`"SELECT 1 "` 与 `"SELECT 1"` 是同一条），
    但比较是**顺序敏感**的：control 列表顺序不同 = 内容不同（列表里的先后
    可能对应结果集/执行语义），记 conflict 而不是 duplicate。
    """
    if str(row.expect) != expect:
        return False
    if expect == "refuse":
        return True
    return ([str(sql).strip() for sql in row.control or []]
            == [str(sql).strip() for sql in control or []])


def corpus_ds(run: dict, ds: str | None) -> str | None:
    """本次运行/采纳的语料数据源：显式 `ds` 优先，KB 级 run 回落 `bench_ds`。

    `alias` / `mapping` 这类抽象家族 run 自身 `ds=None`（版本链是 KB 级的），
    但评测仍需一个数据源来跑语料；P1-5 起由开库校验把条目里的数据源写进
    `run.request["bench_ds"]`（服务端写入，不进客户端可传字段）。只读不写，
    不改变 `run.ds` 本身：版本链、链锁、快照仍按 `ds=None` 定位。
    """
    if ds:
        return str(ds)
    request = (run or {}).get("request") if isinstance(run, dict) else None
    if not isinstance(request, dict):
        return None
    bench_ds = request.get("bench_ds")
    return str(bench_ds) if bench_ds else None


async def _first_ds_id(session, tenant_id: str, kb_id: str) -> str | None:
    """该 KB 启用语料里 ORDER BY ds_id 的第一个数据源；一条语料都没有时 None。"""
    stmt = select(EvolutionCorpusCase.ds_id).where(
        EvolutionCorpusCase.tenant_id == tenant_id,
        EvolutionCorpusCase.kb_id == kb_id,
        EvolutionCorpusCase.enabled == 1,
    ).order_by(EvolutionCorpusCase.ds_id).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def load_corpus(session, tenant_id: str, kb_id: str,
                      ds_id: str | None = None) -> dict:
    """读该 KB + 数据源的启用语料。

    `ds_id` 为空（alias 演化没有数据源维度）时，取该 KB 语料中 `ORDER BY ds_id`
    的第一个数据源再过滤；**一条语料都没有则返回空 cases**（由上层决定失败/空草稿）。
    `target.data_source_id` 始终回填最终解析出的数据源。
    """
    if not ds_id:
        ds_id = await _first_ds_id(session, tenant_id, kb_id)
        if not ds_id:
            return {"target": {"knowledge_base_id": kb_id,
                               "data_source_id": None},
                    "cases": []}
    stmt = select(EvolutionCorpusCase).where(
        EvolutionCorpusCase.tenant_id == tenant_id,
        EvolutionCorpusCase.kb_id == kb_id,
        EvolutionCorpusCase.ds_id == ds_id,
        EvolutionCorpusCase.enabled == 1,
    ).order_by(EvolutionCorpusCase.name)
    rows = list((await session.execute(stmt)).scalars().all())
    return {"target": {"knowledge_base_id": kb_id, "data_source_id": ds_id},
            "cases": [_case_dict(r) for r in rows]}


async def _record_auto_versions(session, tenant_id: str, chains) -> None:
    """借/搬语料**已提交**后，按本次触达的链各入账一版（`source="auto"`）。

    版本是戳不是关键路径：失败只 `logger.warning`，并在失败后 `rollback`
    清掉版本事务的脏尾巴（借/搬已在调用方提交过，这次回滚碰不到它）。
    """
    for kb_id, ds_id in sorted(
            chains, key=lambda item: (str(item[0]), str(item[1]))):
        try:
            await corpus_versions.record(
                session, tenant_id, kb_id, ds_id, source="auto")
        except Exception as exc:  # noqa: BLE001 —— 版本入账是旁路
            logger.warning(
                "语料版本自动入账失败 tenant=%s kb=%s ds=%s: %s",
                tenant_id, kb_id, ds_id, exc, exc_info=True)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 —— 清理失败同样不能影响借/搬
                pass


async def ensure_corpus_for_kb(session, tenant_id: str, kb_id: str,
                               ds_id: str | None = None) -> dict:
    """本次 run 的 KB 没有语料时，从租户内借一份过来（**不抛**）。

    背景：语料按 `kb_id + ds_id` 绑定；重建 KB 后旧行还在，但新库的 run
    `load_corpus` 拿到 0 条 → report 0/0、gate `uncertain`（「报告又空了」）。
    这里在 run 起步时兜底：

    - 目标 KB 已有语料 → 什么都不做（`reason=has_corpus`）；
    - 租户内没有别的语料 → 不伪造（`reason=no_donor`）；
    - 借出方 KB **已删除/不存在** → 把该行**改指**到目标库（名字保持唯一）；
    - 借出方 KB 还活着 → **复制**一条，名字加 `__<kb前8位>` 后缀；
    - 数据源按**源名**映射（重建库后源 id 全变）：目标库没有同名源 → 跳过该条；
      传了 `ds_id`（query_rule）时只补落在这个源上的条。

    返回 `{filled, moved, skipped, reason}`；`filled/moved` 是到目标库的 case 名。
    """
    names = (await session.execute(select(EvolutionCorpusCase.name).where(
        EvolutionCorpusCase.tenant_id == tenant_id,
        EvolutionCorpusCase.kb_id == kb_id,
        EvolutionCorpusCase.enabled == 1,
    ))).scalars().all()
    existing = {str(n) for n in names}
    if existing:
        return {"filled": [], "moved": [], "skipped": [], "reason": "has_corpus"}

    donors = list((await session.execute(select(EvolutionCorpusCase).where(
        EvolutionCorpusCase.tenant_id == tenant_id,
        EvolutionCorpusCase.kb_id != kb_id,
        EvolutionCorpusCase.enabled == 1,
    ).order_by(desc(EvolutionCorpusCase.created_at)))).scalars().all())
    if not donors:
        return {"filled": [], "moved": [], "skipped": [], "reason": "no_donor"}

    live_ids = {str(r) for r in (await session.execute(text(
        "SELECT id FROM knowledge_base.knowledge_info "
        "WHERE tenant_id = :t AND is_deleted = 0"), {"t": tenant_id})).scalars().all()}
    # ⚠️ 源名映射**要包含软删的源**：借出方 KB 被删时它的数据源也软删了，
    # 但 case 里还留着旧 ds_id —— 不查软删行就解析不出源名，会误判 no_match。
    src_rows = (await session.execute(text(
        "SELECT id, name, knowledge_base_id, is_deleted FROM "
        "knowledge_base.knowledge_data_sources WHERE tenant_id = :t"),
        {"t": tenant_id})).all()
    name_by_ds = {str(r[0]): str(r[1] or "") for r in src_rows}
    target_ds_by_name = {
        str(r[1] or ""): str(r[0]) for r in src_rows
        if str(r[2]) == kb_id and not int(r[3] or 0)
    }

    filled: list[str] = []
    moved: list[str] = []
    skipped: list[str] = []
    # 本次**实际写入**的链：filled / moved 都落在 `(目标 kb, 映射后的 ds)`。
    touched: set[tuple] = set()
    for donor in donors:
        target_ds = target_ds_by_name.get(name_by_ds.get(str(donor.ds_id), ""))
        if not target_ds:
            skipped.append(donor.name)
            continue
        if ds_id and target_ds != ds_id:
            continue
        if str(donor.kb_id) not in live_ids:
            donor.kb_id = kb_id
            donor.ds_id = target_ds
            touched.add((kb_id, target_ds))
            moved.append(donor.name)
            continue
        new_name = f"{donor.name}__{kb_id[:8]}"
        if new_name in existing:
            continue
        session.add(EvolutionCorpusCase(
            id=uuid.uuid4().hex, tenant_id=tenant_id, name=new_name,
            question=donor.question, tier=donor.tier, expect=donor.expect,
            control=donor.control, kb_id=kb_id, ds_id=target_ds, enabled=1,
        ))
        existing.add(new_name)
        touched.add((kb_id, target_ds))
        filled.append(new_name)

    if filled or moved:
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return {"filled": [], "moved": [], "skipped": skipped,
                    "reason": "conflict"}
        # 提交成功后才入账：同一条链只记一次；失败只 warning（不影响借/搬）。
        await _record_auto_versions(session, tenant_id, touched)
    return {"filled": filled, "moved": moved, "skipped": skipped,
            "reason": "" if (filled or moved) else "no_match"}


class CorpusService:
    def __init__(self, session) -> None:
        self.session = session

    async def _record_version(self, tenant_id: str, kb_id, ds_id, *,
                              source: str, created_by=None) -> None:
        """语料写入**已提交**后对哪条链入账一版（旁路，绝不影响写入）。

        版本是戳不是关键路径：任何异常只 `logger.warning`（与审计同姿势），
        不改变调用方已提交的数据、也不把异常抛给上层。
        """
        try:
            await corpus_versions.record(
                self.session, tenant_id, kb_id, ds_id,
                source=source, created_by=created_by)
        except Exception as exc:  # noqa: BLE001 —— 版本入账是旁路
            logger.warning(
                "语料版本入账失败 tenant=%s kb=%s ds=%s source=%s: %s",
                tenant_id, kb_id, ds_id, source, exc, exc_info=True)
            # 版本事务的脏尾巴（如迁移缺失引发的 DBAPI 错误）必须清掉，否则
            # 同一 session 后续的审计/操作会撞 PendingRollbackError。语料写入
            # 已在调用方提交过，这次 rollback 不会碰到它。
            try:
                await self.session.rollback()
            except Exception:  # noqa: BLE001 —— 清理失败同样不能影响写入
                pass

    async def list(self, tenant_id: str,
                   kb_ids: list[str] | None = None) -> list[dict]:
        """语料列表。

        `kb_ids` 是**授权集合**（`None` = 不过滤，租户管理员路径；`[]` =
        一个 KB 都管不了 → 直接空页，不发查询）。非 None 时 SQL 追加
        `AND kb_id IN (...)`；`None` 时语句与今天逐字节一致。
        """
        if kb_ids is not None and not kb_ids:
            return []
        stmt = select(EvolutionCorpusCase).where(
            EvolutionCorpusCase.tenant_id == tenant_id
        )
        if kb_ids is not None:
            stmt = stmt.where(EvolutionCorpusCase.kb_id.in_(list(kb_ids)))
        stmt = stmt.order_by(EvolutionCorpusCase.name)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return [_case_dict(r) | {"id": r.id, "kb_id": r.kb_id, "ds_id": r.ds_id,
                                 "enabled": bool(r.enabled)} for r in rows]

    def _build_row(self, tenant_id: str, data: dict):
        """校验 + 构造 ORM 行（不落库）。

        `create` 与 `promote_from_run` 共用这一处校验，避免批量入口悄悄放宽
        `expect` / `control` 的白名单。
        """
        name = str(data.get("name", "")).strip()
        if not name:
            raise InvalidParameterError(message="语料条目名不能为空")
        expect = str(data.get("expect"))
        _validate_expect_control(expect, data.get("control"))
        # 链坐标统一 strip：归属校验用的是 strip 后的值，落库必须同口径，
        # 否则 `" kb1 "` 能过校验却写出一条带空白的脏行。
        kb_id = str(data.get("kb_id") or "").strip()
        ds_id = str(data.get("ds_id") or "").strip()
        return EvolutionCorpusCase(
            id=uuid.uuid4().hex, tenant_id=tenant_id,
            name=name, question=str(data["question"]),
            tier=str(data.get("tier") or "core"), expect=expect,
            control=data.get("control"), kb_id=kb_id, ds_id=ds_id,
            enabled=1 if data.get("enabled", True) else 0,
        )

    async def create(self, tenant_id: str, data: dict) -> dict:
        row = self._build_row(tenant_id, data)
        # 提交后行可能被 expire（甚至被其他路径删掉），先把链坐标留好。
        kb_id, ds_id = row.kb_id, row.ds_id
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise InvalidParameterError(
                message="语料条目名已存在") from None
        await self._record_version(tenant_id, kb_id, ds_id, source="manual",
                                   created_by=data.get("created_by"))
        return {"id": row.id}

    async def promote_from_run(self, tenant_id: str, run_id: str, *,
                               case_ids: list[str] | None = None,
                               user_id=None) -> dict:
        """把 run 里**已验证**的 ad-hoc case 提升为语料行（P1-2 Task 1）。

        - 候选来自**候选表** `evidence.adhoc_cases`（`verify_adhoc` 的回写
          目标，verification 的权威来源）；候选表为空时才回落 `run.patch`；
        - `case_ids` 缺省 = 该 run 全部 verified case；显式给出则只提升这些，
          其中「人工验真 / 已验证但 truth 为空」的 case 记
          `skipped(no_sql_truth)`（不整单失败），其余不存在/未验证 → 1001
          并列出（不静默跳过）；
        - kb / ds 一律取 **run 行**（`patch.kb` / `patch.ds` 是生成补丁时的
          快照，可能过期）；alias run（ds=None）回落 `load_corpus` 解析出的
          数据源；仍解析不到 → 1001（语料行必须绑数据源）；
        - 去重键 = `adhoc_case_id(question)`（R1），同内容记 duplicate、不同
          内容记 conflict，既有行一个字都不改；
        - 新行一次性 commit（原子）；审计一条 `evolution.promote_adhoc`，
          参数只含计数（R5）。`requested` 是**去重/批内归并之前**的候选条数，
          因此恒有 `created + skipped == requested`；`conflicts` 已计入
          `skipped`，只额外单列「为什么跳过」的那一类。
        """
        # 函数内 import：`builders` → `engine` → `corpus_service` 成环，模块级
        # 导入会在 `load_corpus` 定义之前被 `engine` 反向取用而炸掉。
        from .builders import adhoc_case_id
        from .smoke import adhoc_cases

        requested = _normalise_case_ids(case_ids)
        store = RunStore(self.session)
        run = await store.get(tenant_id, run_id)

        # R3：知识库/数据源一律以 **run 行** 为准（`patch.kb` / `patch.ds` 是
        # 补丁生成时的快照，可能已过期），run.ds 为空才回落到语料解析。
        kb_id = str(run.get("kb") or "").strip()
        if not kb_id:
            raise InvalidParameterError(
                message="run 没有知识库信息，无法提升语料")
        ds_id = run.get("ds") or None
        if not ds_id:
            corpus = await load_corpus(self.session, tenant_id, kb_id, None)
            ds_id = (corpus.get("target") or {}).get("data_source_id")
        if not ds_id:
            raise InvalidParameterError(
                message="run 没有数据源，且该知识库的语料也解析不出数据源，"
                        "无法决定提升后的语料归属")

        # 候选 ad-hoc 的权威来源是**候选表**：`verify_adhoc` 只把结论回写到
        # `evolution_candidates.evidence`，`run.patch` 从不被刷新（真实 run 里
        # 那里的 verification 会永远停在 unverified）。候选表查不到任何 case
        # 时才回落 `run.patch`（legacy run 没有候选行）。
        from .candidate_service import CandidateService

        candidates = await CandidateService(self.session).list_for_run(
            tenant_id, run_id)
        cases = _candidate_adhoc_cases(candidates) or adhoc_cases(run)

        # 候选：verified + truth 可用。同一归一化问句只提升第一条，其余显式
        # 记 `duplicate_in_batch`（不静默丢）。
        eligible: dict[str, dict] = {}
        by_id: dict[str, dict] = {}
        no_truth_entries: dict[str, dict] = {}
        no_truth_by_id: dict[str, dict] = {}
        batch_duplicates: list[dict] = []
        for case in cases:
            draft = _promotion_draft(case)
            if draft is None:
                # 人工验真 / verified-but-truth-None：没有 SQL 真值，不能提升。
                # 先收进单独索引，显式请求时记 skipped(no_sql_truth) 而非 1001。
                if _needs_sql_truth(case):
                    question = str(case.get("question") or "").strip()
                    key = adhoc_case_id(question) if question else ""
                    case_id = str(case.get("case_id") or key)
                    if case_id:
                        entry = no_truth_entries.get(key or case_id)
                        if entry is None:
                            entry = {"key": key or case_id,
                                     "case_id": case_id,
                                     "question": question}
                            no_truth_entries[key or case_id] = entry
                        no_truth_by_id.setdefault(case_id, entry)
                        if key:
                            no_truth_by_id.setdefault(key, entry)
                continue
            key = adhoc_case_id(draft["question"])
            case_id = str(case.get("case_id") or key)
            entry = eligible.get(key)
            if entry is None:
                entry = {"key": key, "case_id": case_id, **draft}
                eligible[key] = entry
            else:
                batch_duplicates.append({"case_id": case_id,
                                         "reason": "duplicate_in_batch"})
            # 显式 id 指认：evidence 里的 `case_id` 与惯例的 `adhoc:<sha1>`
            # 两种写法都接受（历史 case 可能沿用旧 id 字段）。
            by_id.setdefault(case_id, entry)
            by_id.setdefault(key, entry)

        if requested is None:
            selected = list(eligible.values())
            skipped: list[dict] = list(batch_duplicates)
            # 同一归一化问句若另有可提升的 case，no_truth 记录已被它覆盖，
            # 不再重复报「无 SQL 真值」。
            skipped.extend(
                {"case_id": entry["case_id"], "reason": "no_sql_truth"}
                for key, entry in no_truth_entries.items()
                if key not in eligible)
            # 审计口径：**批内归并之前**的候选条数 ⇒ 恒有
            # `created + skipped == requested`（conflicts 已计入 skipped）。
            requested_count = len(selected) + len(skipped)
        else:
            missing = [case_id for case_id in requested
                       if case_id not in by_id
                       and case_id not in no_truth_by_id]
            if missing:
                raise InvalidParameterError(
                    message=("这些 ad-hoc case 不存在或尚未验证，无法提升："
                             + "、".join(missing)))
            selected, seen = [], set()
            skipped = []
            for case_id in requested:
                entry = by_id.get(case_id)
                if entry is None:
                    no_truth = no_truth_by_id[case_id]
                    if no_truth["key"] in seen:
                        skipped.append({"case_id": case_id,
                                        "reason": "duplicate_in_batch"})
                        continue
                    seen.add(no_truth["key"])
                    skipped.append({"case_id": case_id,
                                    "reason": "no_sql_truth"})
                    continue
                if entry["key"] in seen:
                    skipped.append({"case_id": case_id,
                                    "reason": "duplicate_in_batch"})
                    continue
                seen.add(entry["key"])
                selected.append(entry)
            # 显式 id 同理：requested 是**归并前**的条数。
            requested_count = len(requested)

        # 去重索引：读该 (tenant, kb, ds) 的**全部**语料行（含停用的，避免把
        # 一条已停用的语料再建一份）。
        existing_rows = list((await self.session.execute(
            select(EvolutionCorpusCase).where(
                EvolutionCorpusCase.tenant_id == tenant_id,
                EvolutionCorpusCase.kb_id == kb_id,
                EvolutionCorpusCase.ds_id == ds_id,
            ))).scalars().all())
        existing: dict[str, object] = {}
        for row in existing_rows:
            existing.setdefault(
                adhoc_case_id(str(row.question or "")), row)
        # 名字唯一索引是 (tenant_id, name)（跨 KB），撞名时延长 sha1 后缀。
        taken = {str(name) for name in (await self.session.execute(
            select(EvolutionCorpusCase.name).where(
                EvolutionCorpusCase.tenant_id == tenant_id))).scalars().all()
            if name is not None}

        created: list[dict] = []
        conflicts: list[dict] = []
        new_rows: list = []
        for entry in selected:
            key = entry["key"]
            row = existing.get(key)
            if row is not None:
                if _same_content(row, entry["expect"], entry["control"]):
                    skipped.append({"case_id": entry["case_id"],
                                    "reason": "duplicate"})
                else:
                    conflicts.append({"case_id": entry["case_id"],
                                      "question": entry["question"],
                                      "existing_case_id": row.id})
                    skipped.append({"case_id": entry["case_id"],
                                    "reason": "conflict"})
                continue
            name = _promotion_name(key.split(":", 1)[-1], taken)
            taken.add(name)
            new_row = self._build_row(tenant_id, {
                "name": name, "question": entry["question"],
                "tier": PROMOTE_TIER, "expect": entry["expect"],
                "control": entry["control"], "kb_id": kb_id,
                "ds_id": ds_id, "enabled": True,
            })
            self.session.add(new_row)
            existing[key] = new_row
            new_rows.append(new_row)
            created.append({"id": new_row.id, **_case_dict(new_row)})

        # 本次**真的新建了题**的链各记一版：同链只记一次（提交前留好坐标，
        # commit 后行属性可能被 expire）。现有 promote 的 kb/ds 都取自 run 行，
        # 实际只有一条链；键仍按 `(kb, ds)` 去重，防止将来按条目分链时重复记。
        chains: dict[tuple, int] = {}
        for new_row in new_rows:
            key = (new_row.kb_id, new_row.ds_id)
            chains[key] = chains.get(key, 0) + 1

        if new_rows:
            try:
                await self.session.commit()
            except IntegrityError:
                # 撞名（含并发下同一问句的输家）→ 1001。提升是管理员低频
                # 操作，重试即可；绝不覆盖既有行。
                await self.session.rollback()
                raise InvalidParameterError(
                    message="语料条目名已存在") from None

        for (chain_kb_id, chain_ds_id), created_count in chains.items():
            if created_count > 0:
                await self._record_version(tenant_id, chain_kb_id,
                                           chain_ds_id, source="promote",
                                           created_by=user_id)

        params = {"requested": requested_count, "created": len(created),
                  "skipped": len(skipped), "conflicts": len(conflicts)}
        warnings: list[str] = []
        recorded = await audit.record(
            self.session, tenant_id=tenant_id,
            action=PROMOTE_AUDIT_ACTION, outcome="success",
            run_id=run_id, request_params=params, user_id=user_id)
        if recorded is False:
            message = f"审计写入失败：{PROMOTE_AUDIT_ACTION}"
            warnings.append(message)
            try:
                await store.log(tenant_id, run_id, message)
            except Exception:  # noqa: BLE001 —— 运行日志是旁路
                pass

        return {"ok": True, "run_id": run_id, "kb_id": kb_id, "ds_id": ds_id,
                "created": created, "skipped": skipped,
                "conflicts": conflicts, "warnings": warnings}

    async def update(self, tenant_id: str, case_id: str, data: dict) -> dict:
        """白名单更新：`name` / `question` / `tier` / `expect` / `control` / `enabled`。

        ⚠️ `kb_id` / `ds_id` **不可经此入口改写**：跨链移动不是支持的操作。
        闸门只校验过条目**当前**所属 KB —— 允许改链等于让某个 KB 的管理员
        把题挪进别人的 KB / 数据源（越权写）。要换链请删除后重建。
        payload 里**出现**这两个键就 1001（哪怕值与当前相同）：静默忽略会让
        客户端误以为「带上原值」是合法 no-op，也会掩盖真正的越权尝试。
        """
        if "kb_id" in data or "ds_id" in data:
            raise InvalidParameterError(
                message="跨链移动不支持：语料条目的 kb_id / ds_id 不可修改，"
                        "请删除后在新链上重建")
        row = (await self.session.execute(select(EvolutionCorpusCase).where(
            EvolutionCorpusCase.id == case_id,
            EvolutionCorpusCase.tenant_id == tenant_id))).scalars().first()
        if row is None:
            raise ResourceNotFoundError(message="语料条目不存在")
        # 白名单落值；tenant_id / id / kb_id / ds_id 等身份列一律不可经此入口改写。
        if "name" in data:
            row.name = str(data["name"]).strip()
        if "question" in data:
            row.question = str(data["question"])
        if "tier" in data:
            row.tier = str(data["tier"] or "core")
        if "expect" in data:
            row.expect = str(data["expect"])
        if "control" in data:
            row.control = data["control"]
        if "enabled" in data:
            row.enabled = 1 if data["enabled"] else 0
        # 用落值后的有效组合校验（允许只改 control、只改 expect 两种方向）。
        _validate_expect_control(row.expect, row.control)
        # 提交前留好链坐标（commit 后属性可能被 expire）；本入口不改链，
        # 所以不存在「新旧两条链」。
        kb_id, ds_id = row.kb_id, row.ds_id
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise InvalidParameterError(
                message="语料条目名已存在") from None
        await self._record_version(tenant_id, kb_id, ds_id, source="manual")
        return {"id": row.id}

    async def delete(self, tenant_id: str, case_id: str) -> dict:
        row = (await self.session.execute(select(EvolutionCorpusCase).where(
            EvolutionCorpusCase.id == case_id,
            EvolutionCorpusCase.tenant_id == tenant_id))).scalars().first()
        if row is not None:
            # 删除并提交后行已 detach/expire，链坐标先留好。
            kb_id, ds_id = row.kb_id, row.ds_id
            await self.session.delete(row)
            await self.session.commit()
            await self._record_version(tenant_id, kb_id, ds_id,
                                       source="manual")
        return {"deleted": True}
