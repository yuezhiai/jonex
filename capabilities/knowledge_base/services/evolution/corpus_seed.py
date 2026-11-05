# -*- coding:utf-8 -*-
"""演化语料自动播种（语料治理线 Task 6）。

两种模式，共用同一套去重 / 审计 / 版本入账：

- `schema`：读该源的 active 编译 schema，逐实体生成
  `{question: "一共有多少个<label>？", expect: "answer",
  control: ["SELECT COUNT(*) FROM <table>"]}`；
- `cases`：导入 YAML / API 题目（`seed_from_cases`），`expect` / `control`
  沿 `corpus_service._validate_expect_control` 的既有口径校验。

**SQL 安全**：schema 模式的表名来自编译 schema（管理员可改，属不可信输入），
必须先过 `relation_discovery._valid_identifier`（带引号 / 非 ASCII / 多段
每段同规则）；SQL 只由 `SELECT COUNT(*) FROM <ident>` 一个模板组成，**不拼接
任何用户文本**。不合格的实体跳过并计入 `skipped`。

**去重**：键是 `adhoc_case_id(question)`（归一化问句的 sha1）。与现有行比对
——同句同内容 `duplicates`、同句异内容 `conflicts`（**绝不覆盖**）；批内同句
只取第一条，其余同样按内容归入 `duplicates` / `conflicts`。

**副作用**：新行一次 commit；提交成功后 `corpus_versions.record(...,
source="seed" | "seed_yaml")`（created=0 不记）；一次调用一条审计
`evolution.seed_corpus`（`resource="evolution_corpus"`）。`dry_run=true`
只算不写：不建行、不提交、不审计、不记版本。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jonex_core.common.exceptions import InvalidParameterError

from ...models.evolution import EvolutionCorpusCase
from ..nl2sql import schema_read
from . import audit, corpus_versions
from .builders import adhoc_case_id
from .corpus_service import _validate_expect_control
from .relation_discovery import _valid_identifier

logger = logging.getLogger(__name__)

#: 审计动作名 / 资源类型（`platform.audit_logs`）。
SEED_AUDIT_ACTION = "evolution.seed_corpus"
SEED_AUDIT_RESOURCE = "evolution_corpus"

#: schema 派生题的 tier（设计 D5）。
SEED_TIER = "single_table"
#: cases 模式缺省 tier（与语料 CRUD 的 `_build_row` 同口径）。
DEFAULT_CASE_TIER = "core"

SEED_NAME_PREFIX = "seed_"
#: 语料名上限（`EvolutionCorpusCase.name` VARCHAR(128)）。
SEED_NAME_MAX = 128

#: 动作模式 → 版本入账 `source`（`corpus_versions.SNAPSHOT_SOURCES`）。
_MODES = ("schema", "cases")
SOURCE_BY_MODE = {"schema": "seed", "cases": "seed_yaml"}


# ---------------------------------------------------------------- 派生
def _entity_label(entity) -> str:
    """`display_name` 优先、回退 `name`；strip 后为空 → 空串。"""
    if not isinstance(entity, dict):
        return ""
    for key in ("display_name", "name"):
        label = str(entity.get(key) or "").strip()
        if label:
            return label
    return ""


def derive_question(entity: dict) -> str | None:
    """实体 → 单表计数问句；没有可用名称 → None。"""
    label = _entity_label(entity)
    if not label:
        return None
    return f"一共有多少个{label}？"


def derive_control(entity: dict) -> tuple[str, str] | None:
    """实体 → `(table, "SELECT COUNT(*) FROM <table>")`；标识符不合格 → None。

    表名取 `source_object_id`、回退 `name`。`_valid_identifier` 是
    **安全边界**：编译 schema 可被 KB 管理员改写，不能当可信字符串拼 SQL。
    """
    if not isinstance(entity, dict):
        return None
    table = str(entity.get("source_object_id") or entity.get("name") or "").strip()
    if not table or not _valid_identifier(table):
        return None
    return table, f"SELECT COUNT(*) FROM {table}"


# ---------------------------------------------------------------- 草稿
def _schema_draft(entity) -> dict | None:
    question = derive_question(entity)
    control = derive_control(entity)
    if not question or control is None:
        return None
    return {"question": question, "tier": SEED_TIER, "expect": "answer",
            "control": [control[1]]}


def _case_draft(case) -> dict | None:
    """一条 cases 载荷 → 草稿；结构不合法（非 dict / 无问句）→ None。

    `expect` / `control` 的语义校验**沿既有口径上抛 1001**（与语料 CRUD
    一致，不静默吞掉一个写错的 expect）。
    """
    if not isinstance(case, dict):
        return None
    question = str(case.get("question") or "").strip()
    if not question:
        return None
    expect = str(case.get("expect") or "")
    control = case.get("control")
    _validate_expect_control(expect, control)
    if expect == "answer":
        sqls = [str(sql).strip() for sql in control]
        if any(not sql for sql in sqls):
            raise InvalidParameterError(message="control 的 SQL 不能为空")
        control = sqls
    else:
        control = None
    tier = str(case.get("tier") or DEFAULT_CASE_TIER).strip() or DEFAULT_CASE_TIER
    return {"question": question, "tier": tier, "expect": expect,
            "control": control}


# ---------------------------------------------------------------- 去重 / 命名
def _content_key(expect, control) -> tuple:
    """内容指纹：`expect` + 逐条 strip 后的 control（顺序敏感，与 promote 同口径）。"""
    expect = str(expect)
    if expect != "answer":
        return (expect, None)
    if not isinstance(control, list):
        return (expect, None)
    return (expect, tuple(str(sql).strip() for sql in control))


def _same_as_row(row, draft: dict) -> bool:
    return _content_key(row.expect, row.control) == _content_key(
        draft["expect"], draft["control"])


def _same_drafts(left: dict, right: dict) -> bool:
    return _content_key(left["expect"], left["control"]) == _content_key(
        right["expect"], right["control"])


def _seed_name(sha1: str, taken: set[str]) -> str:
    """`seed_<sha1[:12]>`；撞名时延长后缀（与 `_promotion_name` 同姿势）。"""
    base = f"{SEED_NAME_PREFIX}{sha1}"
    for width in (12, 20, 40):
        name = f"{SEED_NAME_PREFIX}{sha1[:width]}"
        if name not in taken:
            return name[:SEED_NAME_MAX]
    suffix = 2
    while f"{base}_{suffix}"[:SEED_NAME_MAX] in taken:
        suffix += 1
    return f"{base}_{suffix}"[:SEED_NAME_MAX]


def _new_row(tenant_id: str, kb_id: str, ds_id: str, name: str,
             draft: dict) -> EvolutionCorpusCase:
    return EvolutionCorpusCase(
        id=uuid.uuid4().hex, tenant_id=tenant_id, name=name,
        question=draft["question"], tier=draft["tier"],
        expect=draft["expect"], control=draft["control"],
        kb_id=kb_id, ds_id=ds_id, enabled=1,
    )


async def _record_version(session, tenant_id: str, kb_id: str, ds_id: str, *,
                          source: str, created_by=None) -> str | None:
    """语料已提交后入账一版；版本是旁路，异常只 warning + 清脏事务。

    返回失败原因（成功 / 无需入账 → None），供 `seed()` 汇总进 `warnings`
    —— 版本入账失败对调用方必须是可见的（与审计失败同一个 channel）。
    """
    try:
        await corpus_versions.record(
            session, tenant_id, kb_id, ds_id,
            source=source, created_by=created_by)
    except Exception as exc:  # noqa: BLE001 —— 版本入账不是关键路径
        logger.warning(
            "语料播种版本入账失败 tenant=%s kb=%s ds=%s source=%s: %s",
            tenant_id, kb_id, ds_id, source, exc, exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 —— 清理失败同样不影响已提交的语料
            pass
        return str(exc) or exc.__class__.__name__
    return None


# ---------------------------------------------------------------- 入口
async def seed(session, tenant_id: str, kb_id: str, ds_id: str, *, mode: str,
               cases=None, user_id=None, dry_run: bool = False) -> dict:
    """从编译 schema（`schema`）或显式载荷（`cases`）播种语料。

    返回 `{"created","skipped","duplicates","conflicts","dry_run","items",...}`。
    `skipped` = 生成失败的实体 / 结构非法的载荷；`duplicates` = 同句同内容；
    `conflicts` = 同句异内容（**既有行一个字都不改**）。
    """
    mode = str(mode or "").strip()
    if mode not in _MODES:
        raise InvalidParameterError(message="mode 必须是 schema 或 cases")
    dry_run = bool(dry_run)

    items: list[dict] = []
    drafts: list[dict] = []
    skipped = 0
    if mode == "schema":
        row = await schema_read.load_compiled_row(tenant_id, kb_id, ds_id)
        for entity in (row or {}).get("entity_types") or []:
            draft = _schema_draft(entity)
            if draft is None:
                skipped += 1
                items.append({"question": derive_question(entity),
                              "status": "skipped",
                              "reason": "invalid_entity"})
                continue
            drafts.append(draft)
    else:
        if not isinstance(cases, list):
            raise InvalidParameterError(message="cases 模式必须提供 cases 数组")
        for case in cases:
            draft = _case_draft(case)
            if draft is None:
                skipped += 1
                items.append({"question": "", "status": "skipped",
                              "reason": "invalid_payload"})
                continue
            drafts.append(draft)

    # 现有题**一次**读完：同租户全部行既提供该链的去重索引，也提供跨 KB 的
    # 名称占用集合（唯一索引是 `(tenant_id, name)`）。含停用行，避免把一条
    # 已停用的语料再建一份。
    rows = list((await session.execute(select(EvolutionCorpusCase).where(
        EvolutionCorpusCase.tenant_id == tenant_id))).scalars().all())
    existing: dict[str, object] = {}
    taken: set[str] = set()
    for row in rows:
        name = str(row.name or "").strip()
        if name:
            taken.add(name)
        if (str(row.kb_id) == str(kb_id)
                and str(row.ds_id) == str(ds_id)):
            existing.setdefault(
                adhoc_case_id(str(row.question or "")), row)

    created = duplicates = conflicts = 0
    new_rows: list[EvolutionCorpusCase] = []
    batch: dict[str, dict] = {}
    for draft in drafts:
        key = adhoc_case_id(draft["question"])
        row = existing.get(key)
        if row is not None:
            if _same_as_row(row, draft):
                duplicates += 1
                items.append({"name": row.name,
                              "question": draft["question"],
                              "status": "duplicate",
                              "reason": "duplicate"})
            else:
                conflicts += 1
                items.append({"name": row.name,
                              "question": draft["question"],
                              "status": "conflict",
                              "reason": "conflict",
                              "existing_case_id": row.id})
            continue
        prior = batch.get(key)
        if prior is not None:
            if _same_drafts(prior, draft):
                duplicates += 1
                items.append({"question": draft["question"],
                              "status": "duplicate",
                              "reason": "duplicate_in_batch"})
            else:
                conflicts += 1
                items.append({"question": draft["question"],
                              "status": "conflict",
                              "reason": "conflict_in_batch"})
            continue

        name = _seed_name(key.split(":", 1)[-1], taken)
        taken.add(name)
        batch[key] = draft
        if dry_run:
            items.append({"name": name, "question": draft["question"],
                          "status": "would_create"})
            continue
        new_row = _new_row(tenant_id, kb_id, ds_id, name, draft)
        session.add(new_row)
        existing[key] = new_row
        new_rows.append(new_row)
        created += 1
        items.append({"id": new_row.id, "name": name,
                      "question": draft["question"], "status": "created"})

    version_warning: str | None = None
    if new_rows:
        try:
            await session.commit()
        except IntegrityError:
            # 并发下同一问句的输家 / 名称竞态：绝不覆盖既有行，重试即可。
            await session.rollback()
            raise InvalidParameterError(message="语料条目名已存在") from None
        version_error = await _record_version(
            session, tenant_id, kb_id, ds_id,
            source=SOURCE_BY_MODE[mode], created_by=user_id)
        if version_error:
            version_warning = f"语料版本入账失败：{version_error}"

    request_params = {"mode": mode, "created": created, "skipped": skipped,
                      "duplicates": duplicates, "conflicts": conflicts}
    result = {
        "created": created, "skipped": skipped,
        "duplicates": duplicates, "conflicts": conflicts,
        "dry_run": dry_run, "items": items, "mode": mode,
        "source": SOURCE_BY_MODE[mode],
        "warnings": [version_warning] if version_warning else [],
    }
    if not dry_run:
        recorded = await audit.record(
            session, tenant_id=tenant_id, action=SEED_AUDIT_ACTION,
            outcome="success", run_id=f"corpus:{kb_id}:{ds_id}",
            request_params=request_params, resource=SEED_AUDIT_RESOURCE,
            user_id=user_id)
        if recorded is False:
            message = f"审计写入失败：{SEED_AUDIT_ACTION}"
            result["warnings"].append(message)
            logger.warning(message)
    return result


__all__ = ["SOURCE_BY_MODE", "derive_control", "derive_question", "seed"]
