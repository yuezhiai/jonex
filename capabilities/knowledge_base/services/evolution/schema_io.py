# -*- coding:utf-8 -*-
"""抽象/编译 schema 读写（替代 CLI 的 HTTP 登录）。

`get_row` / `put_row` 是补丁应用的全部 IO 面：直接调用
`AbstractSchemaService` / `OntologyCompiler`，不走 Gateway、不登录。
`apply_patch` / `rollback_patch` 在此之上做「读当前 → 合并 → 乐观锁写 → 回读核对」。

参数名注意：抽象 schema 用 `expected_version`，编译 schema 用
`expected_schema_version`（两边各自的服务签名）。
"""
from __future__ import annotations

import logging
import uuid

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError

from ...repository.ontology_schema_repository import OntologySchemaRepository
from ...repository.ontology_synonym_repository import OntologySynonymRepository
from ..abstract_schema_canonical import (
    canonical_abstract_schema,
    canonical_abstract_snapshot_hash,
)
from ..abstract_schema_service import AbstractSchemaService
from ..ontology_compiler import OntologyCompiler
from .engine import (
    apply_alias_entries,
    apply_constraint_entries,
    apply_constraint_ops,
    apply_mapping_ops,
    apply_relation_entries,
    apply_tool_schema_entries,
    mapping_added_verified,
    rollback_alias_entries,
    rollback_constraint_entries,
    rollback_constraint_ops,
    rollback_mapping_ops,
    rollback_relation_entries,
    rollback_tool_schema_entries,
    tool_schema_state_matches,
)
# 版本链 scope 映射只在 locking 定义一次；这里转出供 adopt/rollback 共用。
from .locking import (
    COMPILED_KINDS, is_abstract_kind, kind_for_scope, scope_for_kind)
from .version_store import canonical_snapshot_hash

logger = logging.getLogger(__name__)

#: 演化类型白名单。写错 kind 会静默落到 compiled 分支（`else`），故入口处显式校验。
#: `mapping` / `tool_schema` 属 abstract 家族（与 `alias` 同链），读/写都走
#: `kind not in _COMPILED_KINDS` 的抽象分支，内层再按 kind 分派变换函数。
_VALID_KINDS = frozenset({"alias", "mapping", "tool_schema", "synonym",
                          "query_rule", "relation", "constraint"})
#: compiled 家族（`query_rule` / `relation` / `constraint`）共用同一存储分支/版本链。
_COMPILED_KINDS = COMPILED_KINDS


def _compiled_payload(row: dict) -> dict:
    """编译行 → `put_row` 需要的整行载荷（`entity_types` / `constraints` 原样）。"""
    row = row or {}
    return {"entity_types": row.get("entity_types") or [],
            "relation_types": row.get("relation_types") or [],
            "constraints": row.get("constraints") or []}


def _row_value(row, key: str):
    """兼容 ORM 行与 dict 行（仓储读口在测试里会被替换成 dict）。"""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


# ────────────────────────────────────────────────────────────────────────────
# 值同义词（synonym）：数据在 `knowledge_base.ontology_synonyms`，不是 schema 行。
# 与 `OntologySynonymService` 同一归一化口径（trim + 全角转半角；匹配再 casefold），
# 但**不经过 service**：演化应用/恢复需要按 terms 精确对账，且必须能软删差额。
# ────────────────────────────────────────────────────────────────────────────

#: 同义词分页读取的页大小（与 `value_aliases.PAGE_SIZE` 同口径）。
_SYNONYM_PAGE_SIZE = 500

#: canonical 长度上限（与 `dtos.ontology_synonym.MAX_TERM_LENGTH` 同口径）。
_SYNONYM_MAX_CANONICAL_LENGTH = 128


def _bump_value_alias_generation(tenant_id: str, kb: str) -> None:
    """同义词行提交后推进 ② 值同义词缓存写代号（失败只 warning）。

    演化应用/恢复/回滚都**绕过** `OntologySynonymService`，不会走 service 的
    写代号；漏掉这里会让同进程缓存在整个 TTL 内继续返回旧同义词（候选验真
    读旧值、回滚后仍能命中已删组）。双 bump 无副作用。
    """
    try:
        from ..nl2sql import value_aliases

        value_aliases.bump_write_generation(tenant_id, kb)
    except Exception as exc:  # noqa: BLE001 —— 缓存失效是旁路
        logger.warning("同义词缓存写代号推进失败 tenant=%s kb=%s: %s",
                       tenant_id, kb, exc)


def _synonym_display(value) -> str:
    """展示/存储归一：trim + 全角转半角（保留大小写）。"""
    text = str(value or "").strip()
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:
            code -= 0xFEE0
        out.append(chr(code))
    return "".join(out).strip()


def _synonym_term_key(value) -> str:
    """去重/匹配键：展示归一后再 casefold。"""
    return _synonym_display(value).casefold()


def _require_synonym_kb_level(ds: str | None) -> None:
    """synonym 是 KB 级链：任何入口收到非 NULL `ds` 都直接拒绝。

    版本表另有 shape CHECK 兜底，但这里先失败能给调用方明确的 1001，而不是
    等到写版本行才撞约束/留下半套数据。
    """
    if ds is not None:
        raise InvalidParameterError(
            message="synonym 是 KB 级版本链，data_source_id 必须为空")


def _normalize_synonym_terms(raw) -> tuple[list[str], tuple[str, ...]]:
    """一组原始词 → (去重保序的展示词, 排序后的归一键元组)。"""
    if not isinstance(raw, list):
        return [], ()
    display: list[str] = []
    keys: set[str] = set()
    for value in raw:
        term = _synonym_display(value)
        if not term:
            continue
        key = term.casefold()
        if key in keys:
            continue
        keys.add(key)
        display.append(term)
    return display, tuple(sorted(keys))


def _terms_key(terms) -> tuple[str, ...]:
    """已存词列表的匹配键（顺序无关）。"""
    if not isinstance(terms, list):
        return ()
    keys = {_synonym_term_key(t) for t in terms}
    return tuple(sorted(k for k in keys if k))


def _row_terms(row) -> list:
    terms = row.get("terms") if isinstance(row, dict) else getattr(
        row, "terms", None)
    return terms if isinstance(terms, list) else []


def _row_canonical(row) -> str:
    value = row.get("canonical") if isinstance(row, dict) else getattr(
        row, "canonical", None)
    return _synonym_display(value)


def _synonym_group_from_row(row) -> dict:
    """ORM 行 / dict → 快照组（只有 `terms` / `canonical`，不带 id/时间戳）。"""
    return {"terms": [str(t) for t in _row_terms(row)],
            "canonical": _row_canonical(row)}


def _normalize_synonym_group(group) -> dict | None:
    """快照/备份里的一组 → 规范形状；非法（非 dict / 无词）返回 None。"""
    if not isinstance(group, dict):
        return None
    terms, _ = _normalize_synonym_terms(group.get("terms"))
    if not terms:
        return None
    return {"terms": terms,
            "canonical": _synonym_display(group.get("canonical"))}


def _sorted_synonym_groups(groups) -> list[dict]:
    """规范化 + 稳定排序：版本 hash 必须与读出顺序无关。"""
    normalized = [g for g in (_normalize_synonym_group(x)
                              for x in (groups or [])) if g is not None]
    return sorted(normalized,
                  key=lambda g: (_terms_key(g["terms"]), g["canonical"]))


async def _synonym_rows(repo, tenant_id: str, kb: str) -> list:
    """分页读到 `total`（**不走进程缓存**：版本快照必须读实时）。"""
    rows: list = []
    offset = 0
    while True:
        items, total = await repo.list_by_kb(
            tenant_id, kb, offset, _SYNONYM_PAGE_SIZE)
        items = list(items or [])
        rows.extend(items)
        offset += len(items)
        if not items or offset >= int(total or 0):
            break
    return rows


async def _load_synonym_groups(tenant_id: str, kb: str) -> list[dict]:
    """该 KB 全部未删组（规范形状 + 稳定排序）。"""
    async with get_db_session() as session:
        rows = await _synonym_rows(
            OntologySynonymRepository(session), tenant_id, kb)
    return _sorted_synonym_groups(
        [_synonym_group_from_row(row) for row in rows])


async def _apply_synonym_patch(tenant_id: str, patch: dict,
                               before_write=None) -> dict:
    """同义词补丁：按 terms upsert，`op=delete` 软删；返回既有 apply 形状。"""
    kb = patch["kb"]
    _require_synonym_kb_level(patch.get("ds"))
    entries = [e for e in patch.get("entries") or [] if isinstance(e, dict)]
    async with get_db_session() as session:
        repo = OntologySynonymRepository(session)
        rows = await _synonym_rows(repo, tenant_id, kb)
        backup = {"synonyms": _sorted_synonym_groups(
            [_synonym_group_from_row(row) for row in rows])}
        index: dict[tuple, object] = {}
        # term 归一键 → 占用它的 terms 集合键（阻止「一词多组」绕过 service 不变量）。
        owner: dict[str, tuple] = {}
        for row in rows:
            group_key = _terms_key(_row_terms(row))
            index.setdefault(group_key, row)
            for term_key in group_key:
                owner.setdefault(term_key, group_key)

        added: list[dict] = []
        skipped: list[dict] = []
        for entry in entries:
            terms, keys = _normalize_synonym_terms(entry.get("terms"))
            canonical = _synonym_display(entry.get("canonical"))
            op = str(entry.get("op") or "add").strip().lower()
            if not terms:
                skipped.append({"terms": terms, "canonical": canonical,
                                "why": "empty_terms"})
                continue
            key = tuple(keys)
            if op == "delete":
                # delete 只按 terms 定位，不要求 canonical（删除时它不参与匹配）。
                row = index.pop(key, None)
                if row is None:
                    skipped.append({"terms": terms, "canonical": canonical,
                                    "op": "delete", "why": "not_found"})
                    continue
                before = _synonym_group_from_row(row)
                await repo.delete_soft(row)
                # 软删后该组不再占用这些词：同批后续条目可以合法地复用。
                for term_key in key:
                    if owner.get(term_key) == key:
                        owner.pop(term_key, None)
                added.append({"terms": terms, "canonical": canonical,
                              "op": "delete", "before": before})
                continue
            if not canonical:
                skipped.append({"terms": terms, "canonical": canonical,
                                "why": "empty_canonical"})
                continue
            if len(canonical) > _SYNONYM_MAX_CANONICAL_LENGTH:
                skipped.append({"terms": terms, "canonical": canonical,
                                "why": "canonical_too_long"})
                continue
            if op not in ("add", "upsert", "update"):
                skipped.append({"terms": terms, "canonical": canonical,
                                "op": op, "why": "unknown_op"})
                continue
            # 同一 terms 集合 = 正常 upsert；任一 term 已属于另一集合 → 拒绝。
            clash = [term_key for term_key in key
                     if owner.get(term_key, key) != key]
            if clash:
                skipped.append({"terms": terms, "canonical": canonical,
                                "why": "cross_group_duplicate",
                                "term": clash[0]})
                continue
            row = index.get(key)
            if row is None:
                obj = await repo.create(
                    id=uuid.uuid4().hex, tenant_id=tenant_id,
                    knowledge_base_id=kb, terms=terms, canonical=canonical)
                index[key] = obj
                for term_key in key:
                    owner.setdefault(term_key, key)
                added.append({"terms": terms, "canonical": canonical,
                              "op": "add"})
            elif _row_canonical(row) == canonical:
                # 幂等：同 terms、同 canonical → 不写库
                skipped.append({"terms": terms, "canonical": canonical,
                                "why": "exists"})
            else:
                before = _row_canonical(row)
                row.canonical = canonical
                added.append({"terms": terms, "canonical": canonical,
                              "op": "update", "before": before})

        if not added:
            raise RuntimeError("应用阶段零条生效（全被跳过）——不写库")
        patch.setdefault("rollback", {})["added"] = added
        patch["skipped"] = (patch.get("skipped") or []) + skipped
        if before_write is not None:
            await before_write(backup, patch)
        await session.commit()
        _bump_value_alias_generation(tenant_id, kb)
    return {"backup": backup, "added": added, "skipped": skipped}


async def _reconcile_synonym_snapshot(tenant_id: str, kb: str,
                                      groups) -> list[dict]:
    """按快照重建同义词表：快照有 → upsert，快照无的未删组 → 软删。

    返回**重建前**的规范内容（hash 校验失败时用它 best-effort 回写）。
    软删只置 `is_deleted=1`，保留历史。
    """
    target = _sorted_synonym_groups(groups)
    target_index = {_terms_key(g["terms"]): g for g in target}
    async with get_db_session() as session:
        repo = OntologySynonymRepository(session)
        rows = await _synonym_rows(repo, tenant_id, kb)
        previous = _sorted_synonym_groups(
            [_synonym_group_from_row(row) for row in rows])
        index: dict[tuple, object] = {}
        for row in rows:
            index.setdefault(_terms_key(_row_terms(row)), row)
        for key, group in target_index.items():
            row = index.get(key)
            if row is None:
                await repo.create(
                    id=uuid.uuid4().hex, tenant_id=tenant_id,
                    knowledge_base_id=kb, terms=list(group["terms"]),
                    canonical=group["canonical"])
            else:
                # 恢复是权威写：terms 的顺序/大小写也按快照复原（hash 口径）。
                row.terms = list(group["terms"])
                row.canonical = group["canonical"]
        for key, row in index.items():
            if key not in target_index:
                await repo.delete_soft(row)
        await session.commit()
        _bump_value_alias_generation(tenant_id, kb)
    return previous


async def _restore_synonym_version(tenant_id: str, kb: str, ds: str | None,
                                   content: dict, version: dict) -> None:
    """`restore_snapshot` 的 synonym 分支：重建 + hash 回读校验。"""
    _require_synonym_kb_level(ds)
    groups = content.get("synonyms")
    if not isinstance(groups, list):
        raise SnapshotRestoreError("synonym 快照内容必须是 synonyms 列表")
    before = await _reconcile_synonym_snapshot(tenant_id, kb, groups)
    rebuilt = await snapshot_for(tenant_id, "synonym", kb, ds)
    expected = version.get("snapshot_hash")
    actual = canonical_snapshot_hash(rebuilt)
    if actual != expected:
        try:
            await _reconcile_synonym_snapshot(tenant_id, kb, before)
        except Exception as exc:  # noqa: BLE001 —— 回写失败不盖住原始校验错误
            logger.warning(
                "同义词快照恢复失败后回写写前内容失败 kb=%s: %s", kb, exc)
        raise SnapshotRestoreError(
            f"快照恢复校验失败：恢复后 hash={actual} 与版本 hash={expected} 不一致")


async def _rollback_synonym_patch(tenant_id: str, patch: dict) -> bool:
    """按 added 逆操作：新建→软删、更新→恢复旧 canonical、删除→重建一组。

    软删的历史行不物理复活（保留审计），删除回滚等价于**新插一组同样的词**。
    """
    kb = patch["kb"]
    _require_synonym_kb_level(patch.get("ds"))
    added = (patch.get("rollback") or {}).get("added") or []
    if not added:
        return False
    async with get_db_session() as session:
        repo = OntologySynonymRepository(session)
        rows = await _synonym_rows(repo, tenant_id, kb)
        index: dict[tuple, object] = {}
        for row in rows:
            index.setdefault(_terms_key(_row_terms(row)), row)
        changed = False
        for item in reversed(added):
            terms, keys = _normalize_synonym_terms(item.get("terms"))
            key = tuple(keys)
            op = str(item.get("op") or "add")
            wanted = _synonym_display(item.get("canonical"))
            if op == "add":
                row = index.pop(key, None)
                if row is not None and _row_canonical(row) == wanted:
                    await repo.delete_soft(row)
                    changed = True
            elif op == "update":
                row = index.get(key)
                if row is not None and _row_canonical(row) == wanted:
                    row.canonical = _synonym_display(item.get("before"))
                    changed = True
            elif op == "delete":
                # 恢复**删除前**的内容（terms + canonical），不是 entry 里
                # 可能不同/为空的 canonical；before 缺失才回退 entry 值。
                before = item.get("before")
                if isinstance(before, dict):
                    before_terms, before_keys = _normalize_synonym_terms(
                        before.get("terms"))
                    if before_terms:
                        terms, key = before_terms, tuple(before_keys)
                        wanted = _synonym_display(before.get("canonical"))
                if key in index:
                    continue
                obj = await repo.create(
                    id=uuid.uuid4().hex, tenant_id=tenant_id,
                    knowledge_base_id=kb, terms=terms, canonical=wanted)
                index[key] = obj
                changed = True
        if changed:
            await session.commit()
            _bump_value_alias_generation(tenant_id, kb)
    return changed


async def columns_by_source(
        tenant_id: str, kb: str,
        ds_ids: set[str]) -> dict[tuple[str, str], dict[str, str]]:
    """指定数据源的「实体 → 列」索引：`{(ds, entity.casefold()): {列 → slug}}`。

    值里把列 slug 的 casefold 与 `physical_name` 的 casefold 都映射回**规范
    slug**（`name`；slug 优先，物理名只在没有同形 slug 时兜底）。mapping 的
    `add` 用它把人工填的物理列名 / 大小写变体归一成落库列名。

    只读一次 `list_compiled_schemas`，只保留 `ds_ids` 里的源；没有 compiled 行
    的源直接缺席（`plan_mapping_ops` 据此报 `source_not_found`）。
    """
    wanted = {str(ds).strip() for ds in (ds_ids or set()) if str(ds).strip()}
    if not wanted:
        return {}
    async with get_db_session() as session:
        rows = await OntologySchemaRepository(session).list_compiled_schemas(
            tenant_id, kb)
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows or []:
        ds = str(_row_value(row, "data_source_id") or "").strip()
        if not ds or ds not in wanted:
            continue
        for entity in _row_value(row, "entity_types") or []:
            if not isinstance(entity, dict):
                continue
            entity_name = str(entity.get("name") or "").strip()
            if not entity_name:
                continue
            colmap = out.setdefault((ds, entity_name.casefold()), {})
            for attr in entity.get("attributes") or []:
                if not isinstance(attr, dict):
                    continue
                slug = str(attr.get("name") or "").strip()
                if not slug:
                    continue
                colmap[slug.casefold()] = slug
                physical = str(attr.get("physical_name") or "").strip()
                if physical:
                    colmap.setdefault(physical.casefold(), slug)
    return out


class SnapshotRestoreError(RuntimeError):
    """快照恢复的**内容性**失败：content 为空 / 写后 hash 与版本不一致。

    单独分类是为了让回滚路径只把这一类错误映射成 409/1003；乐观锁冲突、
    DB 故障等其它异常必须原样上抛（500），不能被伪装成「回滚恢复失败」。
    继承 `RuntimeError` 保持既有调用方的兼容口径（如 `recover_stale`）。
    """


def _require_kind(kind: str) -> None:
    if kind not in _VALID_KINDS:
        raise InvalidParameterError(f"未知的演化类型: {kind}")


async def get_row(tenant_id: str, kind: str, kb: str, ds: str | None) -> dict:
    _require_kind(kind)
    if kind == "synonym":
        # 同义词没有 schema 行：返回 `{"synonyms": [...]}` 供快照/读口使用。
        # 没有版本号（None）—— 乐观锁由链锁 + 行级对账承担。
        _require_synonym_kb_level(ds)
        return {"synonyms": await _load_synonym_groups(tenant_id, kb)}
    if kind in _COMPILED_KINDS:
        data = await OntologyCompiler().get_compiled_schema(tenant_id, kb, ds)
    else:
        data = await AbstractSchemaService().get(tenant_id, kb)
    return data or {}


async def put_row(tenant_id: str, kind: str, kb: str, ds: str | None,
                  payload: dict, expected_version) -> dict:
    _require_kind(kind)
    if kind == "synonym":
        # 同义词的写入必须能按 terms 对账/软删，走 `_apply_synonym_patch` /
        # `_reconcile_synonym_snapshot`，不能伪装成 schema 整行覆盖。
        raise InvalidParameterError(
            message="synonym 不支持 put_row，请使用 apply_patch / restore_snapshot")
    if kind not in _COMPILED_KINDS:
        return await AbstractSchemaService().save(
            tenant_id, kb, payload["schema"], status=payload["status"],
            expected_version=expected_version,
            # 演化应用 / 版本恢复是权威写：载荷来自**先读后写**的行或版本快照，
            # 内容已含本批意图。关闭「按现存行补落点语义槽」——否则恢复一份
            # 旧快照时，当前行已有的 filter/grain/enabled 会注进旧载荷，
            # 重算 hash 与 snapshot_hash 不等，回滚/恢复恒失败。
            _carry_landing_slots=False)
    return await OntologyCompiler().save_compiled_schema(
        tenant_id, kb, payload["entity_types"], payload["relation_types"],
        constraints=payload.get("constraints"), data_source_id=ds,
        expected_schema_version=expected_version,
        # 演化/恢复的内部写：payload 来自**先读后写**的行或版本快照，内容
        # 已含本批意图。关闭「按现存行补 enabled」——否则恢复一份旧快照时，
        # 当前行已物化的 enabled 会注进旧载荷，重算 hash 与 snapshot_hash
        # 不等，回滚/恢复恒失败。公开客户端保存（画布/YAML）仍默认继承。
        _carry_enabled=False)


async def apply_patch(tenant_id: str, patch: dict, *, before_write=None) -> dict:
    """读当前行 → 合并 → 乐观锁写 → 回读核对。

    `before_write(backup, patch)` 是可选异步回调，在 `patch["rollback"]["added"]`
    与 `patch["skipped"]` 记账之后、`put_row` **之前**调用：调用方用它把备份与
    回滚指针先落库 —— 进程若在 PUT 与收口之间崩掉，重启后仍能凭库里的
    `backup` / `rollback.added` 逆操作（只活在内存里的账会随进程一起消失）。
    回调抛错则不写 schema（异常原样上抛）。
    """
    _require_kind(patch["kind"])
    kind = patch["kind"]
    if kind == "synonym":
        return await _apply_synonym_patch(tenant_id, patch,
                                          before_write=before_write)
    row = await get_row(tenant_id, kind, patch["kb"], patch["ds"])
    if is_abstract_kind(kind):
        if kind == "alias":
            new_schema, added, skipped = apply_alias_entries(
                row.get("schema") or {}, patch["entries"])
        elif kind == "tool_schema":
            # 显式分派：tools 覆盖块没有落点/物理三元组，绝不能落进 mapping
            # 的 else（那会把每条都按 mapping 规则跳过 → 零条生效）。
            new_schema, added, skipped = apply_tool_schema_entries(
                row.get("schema") or {}, patch["entries"])
        else:
            entries = patch.get("entries") or []
            ds_ids = {str(e.get("data_source_id") or "").strip()
                      for e in entries if isinstance(e, dict)}
            new_schema, added, skipped = apply_mapping_ops(
                row.get("schema") or {}, entries,
                columns_by_source=await columns_by_source(
                    tenant_id, patch["kb"], ds_ids))
        payload = {"schema": new_schema, "status": row.get("status")}
    elif kind == "relation":
        new_row, added, skipped = apply_relation_entries(row, patch["entries"])
        payload = _compiled_payload(new_row)
    elif kind == "constraint":
        new_row, added, skipped = apply_constraint_ops(row, patch["entries"])
        payload = _compiled_payload(new_row)
    else:
        new_row, added, skipped = apply_constraint_entries(row, patch["entries"])
        payload = _compiled_payload(new_row)
    if not added:
        raise RuntimeError("应用阶段零条生效（全被跳过）——不写库")
    # 记账**前移到 PUT 之前**：写库可能「其实落了库但回执丢了」，回读核对失败时
    # 调用方仍需拿得到 added 才能回滚；核对通过前写账不会造成误回滚。
    patch.setdefault("rollback", {})["added"] = added
    patch["skipped"] = (patch.get("skipped") or []) + skipped
    if before_write is not None:
        await before_write(row, patch)
    await put_row(tenant_id, patch["kind"], patch["kb"], patch["ds"], payload,
                  row.get("schema_version"))
    verify = await get_row(tenant_id, patch["kind"], patch["kb"], patch["ds"])
    if is_abstract_kind(patch["kind"]):
        if patch["kind"] == "alias":
            have = {(str(e.get("name")), str(a))
                    for e in ((verify.get("schema") or {}).get("entities") or [])
                    for a in (e.get("aliases") or [])}
            ok = all((str(a["concept"]), str(a["alias"])) in have for a in added)
        elif patch["kind"] == "tool_schema":
            # update 比 description / disable 比 enabled=False / enable 比缺键；
            # 同一 name 一个批次里被多次操作时只看最后一条（终态）。
            ok = tool_schema_state_matches(verify.get("schema") or {}, added)
        else:
            # 按落点分组的**终态**核对：同一落点在一批里被多次操作时，中间
            # 记录的 after 是当时那一步的投影，只能拿每组最后一条去比。
            ok = mapping_added_verified(verify.get("schema") or {}, added)
    elif patch["kind"] == "relation":
        have = {str(r.get("name")) for r in verify.get("relation_types") or []
                if isinstance(r, dict)}
        # add 的目标是「在」，remove 的目标是「不在」（P1 收尾）。按 op 分别
        # 核对；add 路径的判据与之前逐字节一致。
        ok = all(
            str(a["name"]) not in have
            if str(a.get("op") or "") == "remove"
            else str(a["name"]) in have
            for a in added)
    elif patch["kind"] == "constraint":
        # update / disable / enable 只改名目标字段，光看 name 在不在等于没核对
        # （目标本来就在）——逐条核对本次操作想达到的状态。
        items = {str(c.get("name")): c for c in verify.get("constraints") or []
                 if isinstance(c, dict)}
        wanted_by_name = {
            str(e.get("name") or ""): e
            for e in patch.get("entries") or []
            if isinstance(e, dict) and str(e.get("op") or "") != "add"}
        ok = True
        for added_item in added:
            name = str(added_item.get("name"))
            op = str(added_item.get("op") or "")
            item = items.get(name)
            if item is None:
                ok = False
                break
            if op == "update":
                wanted = str((wanted_by_name.get(name) or {}).get("expression")
                             or "").strip()
                if str(item.get("expression") or "").strip() != wanted:
                    ok = False
                    break
            elif op == "disable" and item.get("enabled") is not False:
                ok = False
                break
            elif op == "enable" and item.get("enabled") is False:
                ok = False
                break
    else:
        have = {str(c.get("name")) for c in verify.get("constraints") or []
                if isinstance(c, dict)}
        ok = all(str(a["name"]) in have for a in added)
    if not ok:
        raise RuntimeError("回读核对失败：应用未生效（patch 已带 added，可回滚）")
    return {"backup": row, "added": added, "skipped": skipped}


def snapshot_from_row(scope: str, kb: str, ds: str | None,
                      row: dict | None) -> dict:
    """把 schema 行按 scope 包装成快照内容（`snapshot_for` 与恢复兜底共用）。

    只保留内容本身：`schema_version` / 时间戳不进快照。
    """
    content = None
    if isinstance(row, dict) and row:
        if row.get("pruned") is True:
            # 采纳成功后 `run.backup` 被剪枝成 `{"pruned": True,
            # "content_hash": ...}`：只剩指纹，没有可恢复内容。显式判成
            # 「无内容」，绝不把占位 dict 当快照（`restore_source` 必须返回
            # None，调用方才不会拿空内容覆盖线上 schema）。
            return {"scope": scope, "kb": kb, "ds": ds, "content": None}
        if scope == "abstract":
            if row.get("schema") is not None:
                content = {
                    "schema": canonical_abstract_schema(row.get("schema")),
                    "status": row.get("status"),
                }
        elif scope == "synonym":
            # 行形状来自 `get_row` / apply 的 backup：`{"synonyms": [...]}`。
            if isinstance(row.get("synonyms"), list):
                content = {"synonyms": _sorted_synonym_groups(
                    row.get("synonyms"))}
        elif any(key in row for key in
                 ("entity_types", "relation_types", "constraints")):
            content = {
                "entity_types": list(row.get("entity_types") or []),
                "relation_types": list(row.get("relation_types") or []),
                "constraints": list(row.get("constraints") or []),
            }
    return {"scope": scope, "kb": kb, "ds": ds, "content": content}


def snapshot_as_version(snapshot: dict) -> dict:
    """把快照包装成 `restore_snapshot` 所需的「版本形状」（含 hash）。"""
    snapshot = snapshot or {}
    return {**snapshot, "snapshot": snapshot,
            "snapshot_hash": canonical_snapshot_hash(snapshot)}


def restore_source(pre: dict | None, fallback_row: dict | None,
                   scope: str, kb: str, ds: str | None) -> dict | None:
    """构造失败恢复源：优先 `pre` 快照，内容缺失时回退 `run.backup` 行。

    两者都拿不到内容 → None（调用方把 run 收口成 failed，绝不用空内容覆盖）。
    """
    content = (pre or {}).get("content")
    if content is None:
        content = snapshot_from_row(
            scope, kb, ds, fallback_row or {}).get("content")
    if content is None:
        return None
    return snapshot_as_version(
        {"scope": scope, "kb": kb, "ds": ds, "content": content})


async def snapshot_for(tenant_id: str, kind: str, kb: str,
                       ds: str | None) -> dict:
    """版本快照（控制器 S1）：**只装内容**，不装版本号/时间戳。

    形状：`{"scope", "kb", "ds", "content"}`。
    - `abstract`（alias / mapping / tool_schema）：`content =
      {"schema": ..., "status": ...}`；
    - `compiled`（query_rule / relation / constraint）：`content =
      {"entity_types", "relation_types", "constraints"}`；
    - `synonym`：`content = {"synonyms": [{"terms", "canonical"}]}`
      （只装内容，不带 id/时间戳；分页读实时，不走进程缓存）。

    `schema_version` / 时间戳刻意不进快照：它们是行级元数据，写回后必然变化，
    放进去会让「恢复后重算的 hash」永远对不上。版本行号已由 `run.backup` /
    `run.parent.schema_version` 记录。

    读取异常或读到空行 → `content=None`（不阻塞快照）；恢复时 `content is None`
    判失败，绝不用空内容覆盖当前 schema。
    """
    scope = scope_for_kind(kind)
    if kind == "synonym":
        # 在读口之前拒绝，避免被下面的宽异常吞成 `content=None`。
        _require_synonym_kb_level(ds)
    try:
        row = await get_row(tenant_id, kind, kb, ds)
    except Exception as exc:  # noqa: BLE001 —— 快照允许内容缺失
        logger.warning("读取快照内容失败 kind=%s kb=%s ds=%s: %s",
                       kind, kb, ds, exc)
        row = None
    return snapshot_from_row(scope, kb, ds, row)


async def restore_snapshot(tenant_id: str, version: dict) -> None:
    """按版本快照写回当前行，并做 **hash 回读校验**（控制器 S2 / spec §7）。

    1. 读当前行拿 `schema_version`（乐观锁的 expected_version）；
    2. `put_row` 写回快照内容；
    3. 用与 `snapshot_for` 完全相同的规则重建内容，计算规范化 hash，必须等于
       版本的期望 hash，否则 `SnapshotRestoreError`。`compiled` 的期望值取
       `version["snapshot_hash"]`；`abstract` 的历史版本行按「读时注解内容」
       存 hash，不能用于存库真相，故从快照**内容**重算（写端同样先规范化）。

    `content is None` 一律判失败（`SnapshotRestoreError`）：空快照不能覆盖
    线上内容。`put_row` 自身的异常（乐观锁冲突 / DB 故障）原样上抛。
    """
    version = version or {}
    scope = version.get("scope")
    kb = version.get("kb") or version.get("knowledge_base_id")
    ds = version.get("ds")
    snapshot = version.get("snapshot")
    content = (snapshot.get("content")
               if isinstance(snapshot, dict) else None)
    if content is None:
        raise SnapshotRestoreError("快照内容为空，不能用空内容覆盖当前 schema")
    if scope == "synonym":
        await _restore_synonym_version(tenant_id, kb, ds, content, version)
        return
    kind = kind_for_scope(scope)
    if scope == "abstract":
        # 写端也走共享序列化器：旧口径快照里的读时派生键不落进 put 载荷。
        content = {**content,
                   "schema": canonical_abstract_schema(content.get("schema"))}
    current = await get_row(tenant_id, kind, kb, ds)
    current_version = (current or {}).get("schema_version")
    await put_row(tenant_id, kind, kb, ds, content,
                  expected_version=current_version)
    rebuilt = await snapshot_for(tenant_id, kind, kb, ds)
    if scope == "abstract":
        # 旧行 hash 是「注解内容」口径：从内容重算，绝不信任 snapshot_hash 列。
        expected = canonical_abstract_snapshot_hash(version)
        actual = canonical_abstract_snapshot_hash(rebuilt)
    else:
        expected = version.get("snapshot_hash")
        actual = canonical_snapshot_hash(rebuilt)
    if actual != expected:
        # I5：写后校验失败说明线上已经被改成了「不是目标快照」的内容。
        # 用写前读到的行 best-effort 写回，别把半套/漂移的内容留在线上；
        # 回写失败只 warning，原始 hash 错误优先上抛。
        await _restore_before_row(tenant_id, kind, kb, ds, scope, current)
        # abstract 的期望值是从快照 content 重算的（不是版本行的 hash 列），
        # 文案如实区分两种口径。
        expected_label = ("快照内容重算 hash" if scope == "abstract"
                          else "版本 hash")
        raise SnapshotRestoreError(
            f"快照恢复校验失败：恢复后 hash={actual} 与"
            f"{expected_label}={expected} 不一致")


async def _restore_before_row(tenant_id: str, kind: str, kb: str,
                              ds: str | None, scope: str,
                              before_row: dict | None) -> bool:
    """把 `restore_snapshot` 失败前的行内容写回（best-effort，返回是否写过）。

    只在 hash 校验失败时使用：此时唯一能确定的「正确内容」就是写前读到的
    `before_row`。写回失败只记 warning —— 不能盖住原始校验错误。
    """
    content = snapshot_from_row(scope, kb, ds, before_row or {}).get("content")
    if content is None:
        return False
    try:
        latest = await get_row(tenant_id, kind, kb, ds)
        await put_row(tenant_id, kind, kb, ds, content,
                      expected_version=(latest or {}).get("schema_version"))
        return True
    except Exception as exc:  # noqa: BLE001 —— 回写失败不盖住原始异常
        logger.warning("快照恢复失败后回写写前内容失败 kind=%s kb=%s ds=%s: %s",
                       kind, kb, ds, exc)
        return False


async def rollback_patch(tenant_id: str, patch: dict) -> bool:
    _require_kind(patch["kind"])
    if patch["kind"] == "synonym":
        return await _rollback_synonym_patch(tenant_id, patch)
    row = await get_row(tenant_id, patch["kind"], patch["kb"], patch["ds"])
    added = (patch.get("rollback") or {}).get("added") or []
    if not added:
        return False
    kind = patch["kind"]
    if is_abstract_kind(kind):
        current_schema = row.get("schema") or {}
        if kind == "alias":
            new_schema = rollback_alias_entries(current_schema, added)
            # 逆操作后内容没变（条目早已不在）→ 不 PUT，避免无意义的版本递增。
            unchanged = new_schema == {
                **current_schema,
                "entities": current_schema.get("entities") or []}
        elif kind == "tool_schema":
            new_schema = rollback_tool_schema_entries(current_schema, added)
            # 同上：逐键比较（覆盖项早已不在时不留一个空版本）。
            unchanged = new_schema == current_schema
        else:
            new_schema = rollback_mapping_ops(current_schema, added)
            # 同上：逐键比较（落点/槽早已不在时不留一个空版本）。
            unchanged = new_schema == current_schema
        if unchanged:
            return False
        await put_row(tenant_id, kind, patch["kb"], patch["ds"],
                      {"schema": new_schema, "status": row.get("status")},
                      row.get("schema_version"))
        return True
    if kind == "relation":
        new_row = rollback_relation_entries(row, added)
    elif kind == "constraint":
        new_row = rollback_constraint_ops(row, added)
    else:
        new_row = rollback_constraint_entries(row, added)
    payload = _compiled_payload(new_row)
    # 同上：与当前内容逐字段比较（None 与 [] 归一，避免把「本来就空」当成变化）。
    if (payload["entity_types"] == (row.get("entity_types") or [])
            and payload["relation_types"] == (row.get("relation_types") or [])
            and payload["constraints"] == (row.get("constraints") or [])):
        return False
    await put_row(tenant_id, kind, patch["kb"], patch["ds"], payload,
                  row.get("schema_version"))
    return True
