# -*- coding:utf-8 -*-
"""语义版本存储（迁移 024 / 演化台信任闭环 P0）。

版本按**分层链**组织：`abstract` 是 KB 级一条链（`ds` 恒为 None），`compiled`
是每个 DS 一条链（`ds` 必填）。每条链在 `semantic_versions` 里至多一条 `active`
（迁移 024 的 `uq_evo_version_active` 部分唯一索引兜底）。

本模块只做存储原语，不负责采纳/回滚编排：
- `create_candidate` 只写 `candidate`，快照不可变；
- `activate` 在一次提交里推进 active 指针（旧 active → `superseded`）；
- `archive_old` 把同一链上超出保留数的旧版本搬进归档表，**active 永不归档**。

租户隔离：所有读写都带 `tenant_id` 条件；跨租户与不存在一样返回
`ResourceNotFoundError`，不泄露「该版本存在但属于别人」。

SQL 直接走 `sqlalchemy.text()` + 具名参数（与同目录 `run_store.py` 一致）。
JSONB 参数传规范化 JSON 字符串（asyncpg 的 jsonb codec 收 str），读取侧兼容
codec 已解码的 dict 与未解码的 str 两种形态。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from sqlalchemy import text

from jonex_core.common.exceptions import (
    InvalidParameterError,
    ResourceNotFoundError,
)

#: 版本作用域：KB 级抽象 schema / KB 级值同义词 / 单数据源编译 schema。
#: `synonym` 与 `abstract` 一样是 KB 级单链（`data_source_id IS NULL`），
#: 但内容是 `ontology_synonyms` 的对账快照，不是 schema 行。
SCOPES = {"abstract", "synonym", "compiled"}

#: 迁移 024 的版本状态白名单。
STATUSES = {"candidate", "active", "superseded", "rejected"}

_MAIN = "knowledge_base.semantic_versions"
_ARCHIVE = "knowledge_base.semantic_versions_archive"

#: 主表/归档表同构的列（顺序即 SELECT / INSERT ... SELECT 的列序）。
_COLUMNS = (
    "id", "tenant_id", "knowledge_base_id", "scope", "data_source_id",
    "parent_version_id", "source_run_id", "source_version_id", "snapshot",
    "snapshot_hash", "status", "created_by", "legacy", "note", "created_at",
    "activated_at",
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _canonical_json(snapshot: dict) -> str:
    """规范化 JSON：键排序、无多余空白、中文不转义。"""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def canonical_snapshot_hash(snapshot: dict) -> str:
    """快照的规范化 sha256（64 位小写十六进制）。"""
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _as_json(value):
    """读取侧容错：asyncpg 正常返回 dict，但驱动配置差异下可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _validate_scope_ds(scope: str, ds: str | None) -> None:
    """scope 与 data_source_id 的形状校验（与迁移 024 的 CHECK 同口径）。"""
    if scope not in SCOPES:
        raise InvalidParameterError(message=f"非法版本 scope: {scope}")
    if scope in ("abstract", "synonym"):
        if ds is not None:
            raise InvalidParameterError(
                message=f"{scope} 版本的 data_source_id 必须为空")
    elif not ds:
        raise InvalidParameterError(
            message="compiled 版本必须指定 data_source_id")


def _select_columns(alias: str, *, archived: str | None = None,
                    include_snapshot: bool = True) -> str:
    cols = ", ".join(f"{alias}.{name}" for name in _COLUMNS
                     if include_snapshot or name != "snapshot")
    if archived is not None:
        cols += f", {archived} AS archived"
    return cols


def _dedupe_by_id_sql(branches: str) -> str:
    """把「主表分支 UNION ALL 归档分支」包成**按 id 去重**的子查询。

    主表与归档表同 id 并存时（`archive_old` 的 ON CONFLICT 冲突行），整行
    可能不同（`archived` 字面量、`archived_at` 等），单靠 `UNION` 合并不掉。
    这里 `row_number() ... PARTITION BY id ORDER BY archived ASC`：`FALSE`
    （主表）排在 `TRUE`（归档）前，`_rn = 1` 即主表优先。外层显式列名，
    不输出内部 `_rn`。

    ⚠️ 外层列集合必须 ⊂ 内层 select 的列集合：当前两个调用点的内层都按
    `include_snapshot=False` 生成（列表本就不返回完整快照），所以外层也必须
    排除 `snapshot`，否则会引用不存在的 `d.snapshot` 直接报错。
    """
    outer = _select_columns("d", archived="d.archived", include_snapshot=False)
    return (
        f"SELECT {outer} FROM (\n"
        f"  SELECT u.*, row_number() OVER ("
        f"PARTITION BY u.id ORDER BY u.archived ASC) AS _rn\n"
        f"  FROM (\n{branches}\n  ) AS u\n"
        f") AS d WHERE d._rn = 1"
    )


def _chain_where(alias: str, scope: str, ds: str | None,
                 params: dict, *, all_ds: bool = False) -> str:
    """同一条链的过滤条件：tenant 由调用方补，这里只写 kb/scope/ds。

    `all_ds=True` 只对 `compiled` 有意义：省略 `data_source_id` 谓词，返回该
    KB **所有 DS** 的编译版本（前端「版本」页签默认视图）。`abstract` 恒为
    KB 级单链，不受影响。
    """
    clauses = [
        f"{alias}.knowledge_base_id = :kb",
        f"{alias}.scope = :scope",
    ]
    if scope in ("abstract", "synonym"):
        clauses.append(f"{alias}.data_source_id IS NULL")
    elif not all_ds:
        clauses.append(f"{alias}.data_source_id = :ds")
        params["ds"] = ds
    return " AND ".join(clauses)


def _version_dict(row, *, archived: bool = False,
                  include_snapshot: bool = True) -> dict:
    out = {
        "version_id": row.id,
        "tenant_id": row.tenant_id,
        "kb": row.knowledge_base_id,
        "scope": row.scope,
        "ds": row.data_source_id,
        "parent_version_id": row.parent_version_id,
        "source_run_id": row.source_run_id,
        "source_version_id": row.source_version_id,
        "snapshot_hash": row.snapshot_hash,
        "status": row.status,
        "created_by": row.created_by,
        "legacy": int(row.legacy or 0),
        "note": row.note,
        "created_at": _iso(row.created_at),
        "activated_at": _iso(row.activated_at),
        "archived": archived,
    }
    if include_snapshot:
        out["snapshot"] = _as_json(row.snapshot)
    return out


class VersionStore:
    """`knowledge_base.semantic_versions` 的读写原语。"""

    def __init__(self, session) -> None:
        self.session = session

    # ------------------------------------------------------------ 读
    async def active_for(self, tenant_id: str, kb: str, scope: str,
                         ds: str | None) -> dict | None:
        """返回该版本链当前的 active 版本；没有则 None。"""
        _validate_scope_ds(scope, ds)
        params = {"tenant": tenant_id, "kb": kb, "scope": scope}
        where = " AND ".join((
            "sv.tenant_id = :tenant",
            _chain_where("sv", scope, ds, params),
            "sv.status = 'active'",
        ))
        sql = (f"SELECT {_select_columns('sv')} FROM {_MAIN} AS sv "
               f"WHERE {where} LIMIT 1")
        row = (await self.session.execute(text(sql), params)).first()
        return _version_dict(row) if row is not None else None

    async def get(self, tenant_id: str, version_id: str,
                  include_archived: bool = False, *,
                  with_snapshot: bool = True) -> dict:
        """按 id 取版本；先查主表，找不到且 `include_archived` 时再查归档表。

        `with_snapshot=False` 时 **SQL 不 SELECT `snapshot` 列**（返回值也没有
        该键）——详情接口默认不返回完整快照，没必要把大 JSONB 从库里搬出来。
        其它调用点保持默认 True，行为不变。

        跨租户与不存在同口径：`ResourceNotFoundError`。
        """
        params = {"tenant": tenant_id, "version_id": version_id}
        select = _select_columns("sv", include_snapshot=with_snapshot)
        sql = (f"SELECT {select} FROM {_MAIN} AS sv "
               f"WHERE sv.tenant_id = :tenant AND sv.id = :version_id "
               f"LIMIT 1")
        row = (await self.session.execute(text(sql), params)).first()
        if row is not None:
            return _version_dict(row, archived=False,
                                 include_snapshot=with_snapshot)

        if include_archived:
            select = _select_columns("sva", include_snapshot=with_snapshot)
            sql = (f"SELECT {select} FROM {_ARCHIVE} AS sva "
                   f"WHERE sva.tenant_id = :tenant AND sva.id = :version_id "
                   f"LIMIT 1")
            row = (await self.session.execute(text(sql), params)).first()
            if row is not None:
                return _version_dict(row, archived=True,
                                     include_snapshot=with_snapshot)

        raise ResourceNotFoundError(message=f"找不到版本: {version_id}")

    async def list(self, tenant_id: str, kb: str, scope: str,
                   ds: str | None, include_archived: bool = False,
                   limit: int = 50, offset: int = 0, *,
                   all_ds: bool = False) -> dict:
        """列出版本链。默认只查主表；`include_archived=True` 时合并归档表。

        列表是摘要（**SQL 不 SELECT `snapshot` 列**，不是查出来再丢），详情走 `get`。
        合并归档时按 id 去重且**同 id 主表优先**（`row_number` 按
        `archived ASC` 取第一行）。

        `all_ds=True` 仅用于 `scope=compiled`：省略 `data_source_id` 谓词，
        返回该 KB 所有 DS 的编译版本；此时 `ds` 被忽略（可为 None）。
        `abstract` 不受影响，仍按「`ds` 必须为空」校验。
        """
        if all_ds and scope == "compiled":
            ds = None
        else:
            _validate_scope_ds(scope, ds)
        params = {"tenant": tenant_id, "kb": kb, "scope": scope,
                  "limit": max(1, int(limit)), "offset": max(0, int(offset))}
        main_where = " AND ".join((
            "sv.tenant_id = :tenant",
            _chain_where("sv", scope, ds, params, all_ds=all_ds),
        ))
        select = _select_columns("sv", archived="FALSE",
                                 include_snapshot=False)
        main_sql = f"SELECT {select} FROM {_MAIN} AS sv WHERE {main_where}"
        if include_archived:
            archive_where = " AND ".join((
                "sva.tenant_id = :tenant",
                _chain_where("sva", scope, ds, params, all_ds=all_ds),
            ))
            archive_sql = (
                f"SELECT {_select_columns('sva', archived='TRUE', include_snapshot=False)} "
                f"FROM {_ARCHIVE} AS sva WHERE {archive_where}")
            sql = _dedupe_by_id_sql(f"{main_sql}\nUNION ALL\n{archive_sql}")
            sql += "\nORDER BY d.created_at DESC, d.id DESC"
        else:
            sql = f"{main_sql}\nORDER BY created_at DESC, id DESC"
        sql += "\nLIMIT :limit OFFSET :offset"
        rows = (await self.session.execute(text(sql), params)).all()
        return {
            "items": [_version_dict(row, archived=bool(getattr(row, "archived", False)),
                                    include_snapshot=False) for row in rows],
            "limit": params["limit"],
            "offset": params["offset"],
            "include_archived": bool(include_archived),
        }

    async def list_merged(self, tenant_id: str, kb: str, *, ds: str | None,
                          include_archived: bool = False,
                          limit: int = 50, offset: int = 0) -> dict:
        """`scope` 省略时的合并列表：abstract + synonym + compiled 一次 UNION 分页。

        - abstract 分支恒为 KB 级单链（`data_source_id IS NULL`）；
        - synonym 分支（值同义词）同样是 KB 级单链（`data_source_id IS NULL`）：
          它是**独立**版本链，不并进 abstract 的 WHERE，否则那里一条行都不会返回；
        - compiled 分支：`ds` 非空时只取该 DS，省略时取该 KB **所有 DS**
          （与 `list(all_ds=True)` 同口径）；
        - `include_archived=True` 时每个 scope 各再 UNION 归档表；
        - 所有分支统一 `ORDER BY created_at DESC, id DESC`，**在库内一次
          分页**（不再对每个 scope 各取 `offset+limit` 再合并 —— offset 大时
          成本线性增长）。

        列表是摘要（**SQL 不 SELECT `snapshot` 列**）。每条 `archived` 标记来源表。
        `include_archived=True` 时按 id 去重且**同 id 主表优先**（整行可能因
        `archived` 字面量不同而不同，单靠 `UNION` 合并不掉）。
        """
        if ds is not None and not isinstance(ds, str):
            raise InvalidParameterError(message="ds 必须是字符串")
        ds = (ds or "").strip() or None
        params = {"tenant": tenant_id, "kb": kb,
                  "limit": max(1, int(limit)), "offset": max(0, int(offset))}
        if ds:
            params["ds"] = ds

        def _where(alias: str, scope: str) -> str:
            clauses = [
                f"{alias}.tenant_id = :tenant",
                f"{alias}.knowledge_base_id = :kb",
                f"{alias}.scope = '{scope}'",
            ]
            # abstract / synonym 都是 KB 级单链（`data_source_id IS NULL`）；
            # 只有 compiled 才按 ds 过滤（省略 ds = 该 KB 所有 DS）。
            if scope in ("abstract", "synonym"):
                clauses.append(f"{alias}.data_source_id IS NULL")
            elif ds:
                clauses.append(f"{alias}.data_source_id = :ds")
            return " AND ".join(clauses)

        branches = [
            f"SELECT {_select_columns('sv', archived='FALSE', include_snapshot=False)} "
            f"FROM {_MAIN} AS sv WHERE {_where('sv', 'abstract')}",
            f"SELECT {_select_columns('sv', archived='FALSE', include_snapshot=False)} "
            f"FROM {_MAIN} AS sv WHERE {_where('sv', 'synonym')}",
            f"SELECT {_select_columns('sv', archived='FALSE', include_snapshot=False)} "
            f"FROM {_MAIN} AS sv WHERE {_where('sv', 'compiled')}",
        ]
        if include_archived:
            branches.extend([
                f"SELECT {_select_columns('sva', archived='TRUE', include_snapshot=False)} "
                f"FROM {_ARCHIVE} AS sva WHERE {_where('sva', 'abstract')}",
                f"SELECT {_select_columns('sva', archived='TRUE', include_snapshot=False)} "
                f"FROM {_ARCHIVE} AS sva WHERE {_where('sva', 'synonym')}",
                f"SELECT {_select_columns('sva', archived='TRUE', include_snapshot=False)} "
                f"FROM {_ARCHIVE} AS sva WHERE {_where('sva', 'compiled')}",
            ])
        combined = "\nUNION ALL\n".join(branches)
        if include_archived:
            sql = _dedupe_by_id_sql(combined)
            sql += "\nORDER BY d.created_at DESC, d.id DESC"
        else:
            # 无归档时三条分支来自同一张主表、scope 互斥，id 不会重复。
            sql = combined + "\nORDER BY created_at DESC, id DESC"
        sql += "\nLIMIT :limit OFFSET :offset"
        rows = (await self.session.execute(text(sql), params)).all()
        return {
            "items": [_version_dict(row,
                                    archived=bool(getattr(row, "archived", False)),
                                    include_snapshot=False) for row in rows],
            "limit": params["limit"],
            "offset": params["offset"],
            "include_archived": bool(include_archived),
        }

    async def list_for_run(self, tenant_id: str, run_id: str,
                           extra_ids: list[str] | None = None) -> list[dict]:
        """该 run 相关版本：`source_run_id = run_id` ∪ 显式 id（采纳/父版本）。

        只查主表（active 永不归档；`source_run_id` 版本在保留窗口外可能进
        归档，届时此处查不到 —— 调用方按「无版本」处理，不阻塞 RunBundle）。
        返回摘要（不含完整 `snapshot`），按 `created_at` 倒序。
        """
        ids = [str(i) for i in (extra_ids or []) if i]
        params = {"tenant": tenant_id, "run_id": run_id}
        clauses = ["sv.source_run_id = :run_id"]
        if ids:
            clauses.append("sv.id = ANY(CAST(:ids AS varchar[]))")
            params["ids"] = ids
        # 租户条件必须**包住整个 OR**，否则任一额外 id 就能跨租户读版本。
        where = (f"sv.tenant_id = :tenant AND ({' OR '.join(clauses)})")
        sql = (f"SELECT {_select_columns('sv', archived='FALSE')} "
               f"FROM {_MAIN} AS sv WHERE {where} "
               f"ORDER BY sv.created_at DESC, sv.id DESC")
        rows = (await self.session.execute(text(sql), params)).all()
        return [_version_dict(row, archived=False, include_snapshot=False)
                for row in rows]

    # ------------------------------------------------------------ 写
    async def create_candidate(
        self, tenant_id: str, kb: str, scope: str, ds: str | None,
        snapshot: dict, *, parent_version_id: str | None = None,
        source_run_id: str | None = None,
        source_version_id: str | None = None,
        created_by: str | None = None, note: str | None = None,
    ) -> dict:
        """插入一条 `candidate` 版本（不可变快照 + 规范化 hash）。

        `parent_version_id` 非空时必须是**同一条链**（同 `tenant / kb / scope /
        ds`）上的版本：spec §3.1 要求父版本与当前版本同 scope、同
        `data_source_id` 范围。父版本不存在或跨链一律 1001
        （`InvalidParameterError`），不落任何行。
        """
        _validate_scope_ds(scope, ds)
        if parent_version_id:
            try:
                parent = await self.get(tenant_id, parent_version_id,
                                        include_archived=True)
            except ResourceNotFoundError:
                # 校验失败先回滚：别把会话留在 idle-in-transaction 状态。
                await self.session.rollback()
                raise InvalidParameterError(
                    message=f"找不到父版本: {parent_version_id}") from None
            mismatched = []
            if parent.get("kb") != kb:
                mismatched.append("knowledge_base_id")
            if parent.get("scope") != scope:
                mismatched.append("scope")
            if parent.get("ds") != ds:
                mismatched.append("data_source_id")
            if mismatched:
                await self.session.rollback()
                raise InvalidParameterError(
                    message=(f"父版本 {parent_version_id} 与当前版本不在同一条链"
                             f"（不一致维度: {', '.join(mismatched)}）"))
        version_id = "ver_" + uuid.uuid4().hex
        snapshot_hash = canonical_snapshot_hash(snapshot)
        params = {
            "id": version_id,
            "tenant": tenant_id,
            "kb": kb,
            "scope": scope,
            "ds": ds,
            "parent_version_id": parent_version_id,
            "source_run_id": source_run_id,
            "source_version_id": source_version_id,
            "snapshot": _canonical_json(snapshot),
            "snapshot_hash": snapshot_hash,
            "status": "candidate",
            "created_by": created_by,
            "legacy": 0,
            "note": note,
        }
        sql = (f"INSERT INTO {_MAIN} "
               f"(id, tenant_id, knowledge_base_id, scope, data_source_id, "
               f"parent_version_id, source_run_id, source_version_id, snapshot, "
               f"snapshot_hash, status, created_by, legacy, note) VALUES "
               f"(:id, :tenant, :kb, :scope, :ds, :parent_version_id, "
               f":source_run_id, :source_version_id, CAST(:snapshot AS jsonb), "
               f":snapshot_hash, :status, :created_by, :legacy, :note) "
               f"RETURNING created_at")
        try:
            result = await self.session.execute(text(sql), params)
            created = result.first()
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        created_at = created[0] if created is not None else None
        return {
            "version_id": version_id,
            "tenant_id": tenant_id,
            "kb": kb,
            "scope": scope,
            "ds": ds,
            "parent_version_id": parent_version_id,
            "source_run_id": source_run_id,
            "source_version_id": source_version_id,
            "snapshot": snapshot,
            "snapshot_hash": snapshot_hash,
            "status": "candidate",
            "created_by": created_by,
            "legacy": 0,
            "note": note,
            "created_at": _iso(created_at),
            "activated_at": None,
            "archived": False,
        }

    async def activate(self, tenant_id: str, version_id: str,
                       previous_id: str | None) -> None:
        """推进 active 指针：旧 active（若有）→ `superseded`，新版本 → `active`。

        **activate 可把归档版本回迁主表**：若主表 UPDATE 命中 0 行（版本已被
        `archive_old` 搬进归档表），在同一事务内把归档行 `INSERT ... SELECT`
        回主表、删除归档行，再重试置 active。归档里也没有（或跨租户）才回滚
        并抛 1002。主表已存在同一 id 时第一次 UPDATE 就会命中，以主表为准，
        不触碰归档表。

        两条 UPDATE（以及可能的回迁 / 重试）都在同一次提交里；都带
        `tenant_id`，旧行只覆盖仍为 `active` 的行，避免误伤
        candidate/rejected。旧行命中 0 是幂等重放的合法情况。
        """
        supersede_sql = (f"UPDATE {_MAIN} AS sv SET status = 'superseded' "
                         f"WHERE sv.id = :previous_id "
                         f"AND sv.tenant_id = :tenant "
                         f"AND sv.status = 'active'")
        activate_sql = (f"UPDATE {_MAIN} AS sv "
                        f"SET status = 'active', activated_at = :activated_at "
                        f"WHERE sv.id = :version_id "
                        f"AND sv.tenant_id = :tenant")
        archive_exists_sql = (
            f"SELECT sva.id FROM {_ARCHIVE} AS sva "
            f"WHERE sva.tenant_id = :tenant AND sva.id = :version_id LIMIT 1")
        # 列顺序与主表/归档表同构（_COLUMNS），回迁后再由上面同一句 UPDATE 激活。
        restore_sql = (
            f"INSERT INTO {_MAIN} ({', '.join(_COLUMNS)}) "
            f"SELECT {_select_columns('sva')} FROM {_ARCHIVE} AS sva "
            f"WHERE sva.tenant_id = :tenant AND sva.id = :version_id "
            f"ON CONFLICT (id) DO NOTHING")
        purge_sql = (f"DELETE FROM {_ARCHIVE} AS sva "
                     f"WHERE sva.tenant_id = :tenant "
                     f"AND sva.id = :version_id")
        try:
            if previous_id:
                # 旧 active 可能已被别的路径置 superseded（幂等重放）：命中 0 行合法。
                await self.session.execute(
                    text(supersede_sql),
                    {"tenant": tenant_id, "previous_id": previous_id})
            key_params = {"tenant": tenant_id, "version_id": version_id}
            params = {**key_params, "activated_at": _now()}
            result = await self.session.execute(text(activate_sql), params)
            if not result.rowcount:
                # 主表没有该 id：可能是 archive_old 把候选搬走了（C1-3）。
                archived = (await self.session.execute(
                    text(archive_exists_sql), key_params)).first()
                if archived is None:
                    # 归档里也没有 → 不存在或跨租户：链不能变成「没有 active」。
                    raise ResourceNotFoundError(
                        message=f"找不到待激活版本: {version_id}")
                await self.session.execute(text(restore_sql), key_params)
                await self.session.execute(text(purge_sql), key_params)
                result = await self.session.execute(text(activate_sql), params)
                if not result.rowcount:
                    raise ResourceNotFoundError(
                        message=f"找不到待激活版本: {version_id}")
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def reject_version(self, tenant_id: str, version_id: str) -> bool:
        """失败收口专用：把 `candidate` / `active` 版本置 `rejected`。

        只在这两条状态上生效：`superseded` 是仍然有效的历史版本，不能被
        失败路径误伤。`active` 出现在「已激活、但收口 CAS 失败」的补偿路径，
        调用方必须紧接着把旧 active 重新激活。命中 0 行（幂等重放 /
        已被并发收口）不报错，返回 False。
        """
        sql = (f"UPDATE {_MAIN} AS sv SET status = 'rejected' "
               f"WHERE sv.id = :version_id AND sv.tenant_id = :tenant "
               f"AND sv.status IN ('candidate', 'active')")
        try:
            result = await self.session.execute(
                text(sql), {"tenant": tenant_id, "version_id": version_id})
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return bool(result.rowcount)

    async def archive_old(self, tenant_id: str, kb: str, scope: str,
                          ds: str | None, keep: int = 50) -> int:
        """把同一链上超出最近 `keep` 个的旧版本搬进归档表；返回搬移行数。

        单事务、四步：

        1. `SELECT id ... ORDER BY created_at DESC, id DESC OFFSET :keep
           FOR UPDATE` **固定**本次要搬移的 id 集合；`FOR UPDATE` 防止并发
           activate 在后续语句之间把某行从非 active 改成 active。
        2. 集合为空 → 直接提交返回 0（不写归档表）。
        3. `INSERT ... SELECT ... WHERE id = ANY(:ids) ON CONFLICT DO NOTHING`：
           归档表已存在同一 id 时不重复写入。
        4. `DELETE ... WHERE id = ANY(:ids) AND status <> 'active'`：即使
           INSERT 因冲突跳过，主表行也一并删掉（避免两表并存）；`status <>
           'active'` 是最后一道保护。

        返回 **DELETE 的 rowcount**（真实从主表搬走的行数）。归档冲突行计入
        删除数，但不会新增归档行。`keep` **只计非 active 版本**（`status <>
        'active'` 进排名），因此一条链最多保留 `keep` 个非 active + 1 个
        active；active 永不进入搬移集合。
        """
        _validate_scope_ds(scope, ds)
        keep = max(0, int(keep))
        chain_params = {"tenant": tenant_id, "kb": kb, "scope": scope}
        main_chain = _chain_where("sv", scope, ds, chain_params)

        select_sql = (
            f"SELECT sv.id FROM {_MAIN} AS sv "
            f"WHERE sv.tenant_id = :tenant AND {main_chain} "
            f"AND sv.status <> 'active' "
            f"ORDER BY sv.created_at DESC, sv.id DESC "
            f"OFFSET :keep FOR UPDATE"
        )
        insert_sql = (
            f"INSERT INTO {_ARCHIVE} "
            f"(id, tenant_id, knowledge_base_id, scope, data_source_id, "
            f"parent_version_id, source_run_id, source_version_id, snapshot, "
            f"snapshot_hash, status, created_by, legacy, note, created_at, "
            f"activated_at, archived_at) "
            f"SELECT sv.id, sv.tenant_id, sv.knowledge_base_id, sv.scope, "
            f"sv.data_source_id, sv.parent_version_id, sv.source_run_id, "
            f"sv.source_version_id, sv.snapshot, sv.snapshot_hash, sv.status, "
            f"sv.created_by, sv.legacy, sv.note, sv.created_at, "
            f"sv.activated_at, :archived_at "
            f"FROM {_MAIN} AS sv "
            f"WHERE sv.tenant_id = :tenant "
            f"AND sv.id = ANY(CAST(:ids AS varchar[])) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        # 冲突行（已存在于归档表）同样删主表：否则两表并存。`status <> 'active'`
        # 防止 UPDATE ... FOR UPDATE 之后、DELETE 之前被并发 activate 的行被误删。
        delete_sql = (
            f"DELETE FROM {_MAIN} AS sv "
            f"WHERE sv.tenant_id = :tenant "
            f"AND sv.id = ANY(CAST(:ids AS varchar[])) "
            f"AND sv.status <> 'active'"
        )
        try:
            result = await self.session.execute(
                text(select_sql), {**chain_params, "keep": keep})
            ids = [row[0] for row in result.all()]
            if not ids:
                await self.session.commit()
                return 0
            params = {**chain_params, "ids": ids, "archived_at": _now()}
            await self.session.execute(text(insert_sql), params)
            deleted = await self.session.execute(text(delete_sql), params)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        return int(deleted.rowcount or 0)


__all__ = [
    "SCOPES",
    "STATUSES",
    "VersionStore",
    "canonical_snapshot_hash",
]
