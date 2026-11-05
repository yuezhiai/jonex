# -*- coding:utf-8 -*-
"""回滚与恢复侧（旧补丁逆操作 / 版本回滚 / 回滚重放）的 mixin。

依赖一律经 `common.<name>` 解析，保证测试对 `adopt_common` 的 patch 生效。
"""
from __future__ import annotations

import logging
import time

from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ResourceConflictError,
    ResourceNotFoundError,
)

from . import adopt_common as common
from .ledger_service import ledger_type_for_kind
from .locking import is_compiled_kind, kind_for_scope

logger = logging.getLogger(f"{__package__}.adopt_service")


def rollback_ledger_identity(kind: str | None, scope: str,
                             ds: str | None, run: dict) -> dict:
    """回滚台账行的身份三键：`entry_kind` / `op` / `ds_id`。

    - `relation` / `constraint`：与采纳行同 kind（写 query_rule 那本），
      另带 `op="rollback"` 作为回滚判据（采纳行没有这个值）；
    - `mapping`（P1-5）：`kind="mapping"`（写 alias 那本）同样带
      `op="rollback"`；KB 级 run 自身 `ds=None`，`ds_id` 取语料解析出的
      数据源（`corpus_ds`，即 `request.bench_ds`）；
    - `tool_schema`（Tool-Schema 线）：`kind="tool_schema"`（写 alias 那本）
      同样带 `op="rollback"`；KB 级变更，`ds_id=None`；
    - 其余（`alias` / `query_rule`）：沿用旧口径 `kind="rollback"`、无 op。

    `kind` 为空时回落 `kind_for_scope(scope)` 的兼容标签（旧 run 行）；
    仅 compiled scope 会带 `ds_id`（与历史台账形状一致）。
    """
    resolved = str(kind or kind_for_scope(scope))
    if resolved == "tool_schema":
        return {"entry_kind": "tool_schema", "op": "rollback", "ds_id": None}
    if resolved == "synonym":
        # 值同义词线：与采纳行同 kind（写 alias 那本），`op="rollback"` 作为
        # 回滚判据；KB 级变更，`ds_id=None`。
        return {"entry_kind": "synonym", "op": "rollback", "ds_id": None}
    if resolved == "mapping":
        return {"entry_kind": "mapping", "op": "rollback",
                "ds_id": common.corpus_ds(run, ds)}
    if resolved in ("relation", "constraint"):
        return {"entry_kind": resolved, "op": "rollback",
                "ds_id": ds if scope == "compiled" else None}
    return {"entry_kind": "rollback", "op": None,
            "ds_id": ds if scope == "compiled" else None}


class AdoptRecoveryMixin:
    # ------------------------------------------------------------ 回滚
    async def rollback(self, tenant_id: str, run_id: str,
                       to_version_id: str | None = None, reason: str = "",
                       user_id: str | None = None) -> dict:
        """两条回滚路径（控制器 V1）：

        - **版本回滚**（信任闭环）：给了 `to_version_id`，或该 run 曾有版本
          **真正上线**（`adopted_version_id` 非空）。
          恢复到任意历史版本 → 生成新的 active 回滚版本 → run `rolled-back`。
        - **旧补丁逆操作**：从未上线过的 run（`adopted_version_id` 为空）→
          原样保留历史行为（逆 `rollback.added`）。

        判定优先级：`to_version_id` → 版本回滚（无论新旧）；否则
        `adopted_version_id` 非空 → 版本回滚；否则旧路径。

        ⚠️ 旧口径还看过 `parent_version_id`，这会把「新 run 仅 evaluated、
        尚未采纳」误判成版本回滚（它只是有一个父版本，什么都没上线）。
        现在新 run 在 create 时就有 parent，必须只认 `adopted_version_id`。
        """
        explicit = common.normalise_version_id(to_version_id)
        target_reason = str(reason or "").strip()

        store = common.RunStore(self.session)
        await store.recover_stale(tenant_id)
        run = await store.get(tenant_id, run_id)

        # 同链快速冲突：同一版本链已有采纳在飞（完整语料基准可能跑几十分钟）
        # 时不进回滚流程 —— 既不用等链锁，也不会在收口时才发现冲突。
        # 已 `rolled-back` 的 run 跳过这道闸（响应丢失后的重放应保持 no-op）；
        # `exclude_run_id` 排除自己：回滚一个 `adopting` run 应得到状态错误，
        # 而不是「另一个采纳（就是它自己）」。
        if run.get("status") != "rolled-back":
            chain_kind = run.get("kind")
            chain_ds = run.get("ds") if is_compiled_kind(chain_kind) else None
            other = await store.chain_adopting(
                tenant_id, run.get("kb"), chain_kind, chain_ds,
                exclude_run_id=run_id)
            if other:
                raise ResourceConflictError(
                    message=f"同一版本链已有采纳正在进行（{other}），稍后再试")

        if not (explicit is not None
                or run.get("adopted_version_id") is not None):
            return await self._legacy_rollback(
                tenant_id, run_id, run, user_id=user_id)

        try:
            scope = common.scope_for_kind(run.get("kind"))
        except ValueError as exc:
            raise InvalidParameterError(message=str(exc)) from exc
        kb = run.get("kb")
        ds = run.get("ds") if scope == "compiled" else None

        # 与 adopt / runner 的 apply→candidate→rollback 窗口同一把链锁。
        async with common.chain_lock(
                common.advisory_key(tenant_id, kb, scope, ds)):
            # 锁内重读：状态判定必须发生在拿到锁之后。
            run = await store.get(tenant_id, run_id)
            return await self._rollback_version_locked(
                tenant_id, run_id, run, scope=scope, kb=kb, ds=ds,
                explicit=explicit, reason=target_reason, user_id=user_id)

    async def _legacy_rollback(self, tenant_id: str, run_id: str,
                               run: dict, *, user_id=None) -> dict:
        """迁移前旧 run：逆操作本批 `rollback.added`（行为与历史一致）。

        `schema_io.rollback_patch` 的内容幂等由它自己保证（逆操作后内容没变就
        不 PUT）。无论它有没有真写库，逆操作完成即清账 —— 与旧 CLI 的幂等口径
        一致。
        """
        store = common.RunStore(self.session)
        warnings: list[str] = []
        active = await store.has_active(tenant_id)
        if active:
            raise ResourceConflictError(
                message="有演化运行正在进行，稍后再试")
        allowed = common._ROLLBACK_STATUSES
        if (run.get("status") == "failed"
                and run.get("stage") in common._FAILED_ROLLBACK_STAGES):
            # PUT 已成功过的 failed（applied / candidate-ran）：schema 里可能
            # 真留着本批条目，必须允许用户逆操作把它清掉。
            allowed = common._ROLLBACK_STATUSES | {"failed"}
        common._require_status(run, allowed, "回滚")
        patch = run.get("patch") if isinstance(run.get("patch"), dict) else {}
        added = (patch.get("rollback") or {}).get("added") or []
        if not added:
            # 文案刻意不含「已回滚 + 数字 + 条」：前端 `isRollbackDone` 只认
            # N≥1 的字面量，这句必须落进 no-op 分支（未生效/已回滚过）。
            return {
                "ok": True, "rc": 0,
                "stdout_tail": ("库中已无本批条目（未生效或已回滚过）"
                                "——未写库，已清账"),
                "run": run,
                "patch": run.get("patch"),
                "rolled_back": 0, "count": 0,
                "warnings": list(warnings),
            }

        wrote = await common.schema_io.rollback_patch(tenant_id, patch)
        # 值同义词：逆操作已改 `ontology_synonyms`，读口缓存必须失效。
        # 只在真的写了库时推进（False = 内容幂等，缓存也可继续用）。
        if wrote:
            common.bump_value_aliases(run.get("kind"), tenant_id, run.get("kb"))
        # `rollback_patch` 的 False = 内容幂等（条目早已不在 / 本来就没写）：
        # 此时一个条目都没删，绝不能按 `added` 的长度虚报「已回滚 N 条」。
        count = len(added) if wrote else 0
        patch["rollback"] = {**(patch.get("rollback") or {}), "added": []}
        ok = await store.update(
            run_id, tenant_id=tenant_id, status="rolled-back",
            stage="rolled-back", patch=patch,
            expect_statuses=allowed)
        if not ok:
            raise ResourceConflictError(
                message=f"{run_id} 状态已变化，回滚结果未能记账")
        await common._stage_log(store, tenant_id, run_id, "rolled_back",
                         picked_count=count)
        # I7：旧补丁逆操作与版本回滚同口径写审计（legacy=true 让审计能区分
        # 两条路径）；失败进 warnings + run_log，不影响已完成的回滚。
        await common._record_audit(
            store, self.session, tenant_id=tenant_id, run_id=run_id,
            action="evolution.rollback", outcome="success",
            request_params={"legacy": True, "count": count},
            user_id=user_id, warnings=warnings)
        updated = await store.get(tenant_id, run_id)
        return {
            "ok": True, "rc": 0,
            "stdout_tail": (f"已回滚 {count} 条" if wrote else
                            "库中已无本批条目（未生效或已回滚过）"
                            "——未写库，已清账"),
            "run": updated,
            "patch": updated.get("patch"),
            "rolled_back": count, "count": count,
            "warnings": list(warnings),
        }

    async def _rollback_version_locked(self, tenant_id: str, run_id: str,
                                       run: dict, *, scope: str, kb: str,
                                       ds: str | None,
                                       explicit: str | None, reason: str,
                                       user_id) -> dict:
        """持链锁后的版本回滚主体（控制器 V2）。

        顺序：状态判定（`rolled-back` 幂等重放 / 其它非 `adopted` 409）→
        目标版本（显式或 run.parent）→ 同链校验 → hash/内容校验（**写库前**）→
        当前 active 已是目标则 no-op → `restore_snapshot` → 新建回滚版本并激活 →
        run CAS `adopted → rolled-back` → 台账 + 审计 → 归档 best-effort。

        已知限制（P0，I5）：函数内任一步异常都会补偿（恢复旧 active 快照、新版本
        rejected、active 切回旧版本）；但**跨进程崩溃**窗口（例如 restore 已提交、
        create/activate 之前进程被杀）没有可识别的过渡态，重启后不会自动补偿。
        此时需人工核对 active 指针与线上内容（P1 再引入过渡态/对账任务）。
        """
        store = common.RunStore(self.session)
        warnings: list[str] = []
        started = time.monotonic()
        status = run.get("status")

        if status == "rolled-back":
            return await self._rollback_replay(
                tenant_id, run, scope=scope, kb=kb, ds=ds, explicit=explicit)
        if status != "adopted":
            raise ResourceConflictError(
                message=(f"{run_id} 状态是 {status!r}，只有 adopted 的 run "
                         "允许版本回滚（smoke-failed / failed 为终态）"))

        target_id = explicit or run.get("parent_version_id")
        if not target_id:
            raise ResourceConflictError(
                message=(f"{run_id} 没有父版本（legacy run），"
                         "必须显式指定 to_version_id"))

        versions = common.VersionStore(self.session)
        try:
            target = await versions.get(tenant_id, target_id,
                                        include_archived=True)
        except ResourceNotFoundError as exc:
            raise ResourceConflictError(message=str(exc)) from exc

        common._require_same_chain(target, scope=scope, kb=kb, ds=ds)

        # 只接受已评估过的版本：`candidate` 可能尚未评估，`rejected` 是 smoke /
        # 应用失败的内容 —— 恢复它们等于绕过门禁把未证明/已知失败的内容写上线。
        target_status = target.get("status")
        if target_status not in common._ROLLBACK_TARGET_STATUSES:
            raise ResourceConflictError(
                message=(f"目标版本 {target_id} 状态是 {target_status!r}，"
                         "只有 active / superseded 的版本允许回滚"))

        snapshot = target.get("snapshot")
        content = snapshot.get("content") if isinstance(snapshot, dict) else None
        if content is None:
            raise ResourceConflictError(
                message=f"目标版本 {target_id} 的快照内容为空，不能回滚")
        # 写库前先自校验：快照与 hash 不一致一律 409，绝不先恢复再失败。
        if common.canonical_snapshot_hash(snapshot) != target.get("snapshot_hash"):
            raise ResourceConflictError(
                message=(f"目标版本 {target_id} 的快照 hash 校验失败"
                         "（快照被篡改或不完整）"))

        legacy_target = bool(target.get("legacy"))
        archived_target = bool(target.get("archived"))
        if legacy_target:
            warnings.append(
                f"目标版本 {target_id} 是迁移前的 legacy 版本（未经过门禁），"
                "请确认后再继续")

        current = await versions.active_for(tenant_id, kb, scope, ds)
        current_id = (current or {}).get("version_id")
        if current_id == target_id:
            # 线上内容已经在目标版本上：纯 no-op，不新建版本、不写 run。
            return {
                "ok": True, "rc": 0,
                "stdout_tail": "当前已是目标版本——未写库",
                "run": run,
                "version_id": target_id,
                "target_version_id": target_id,
                "legacy": legacy_target,
                "archived": archived_target,
                "rolled_back": 0, "count": 0,
                "warnings": list(warnings),
                "idempotent_replay": False,
            }

        # 有 run 行就用它自己的 kind（compiled 家族内容形状一致）；只有拿不到
        # kind 的旧行才回落到 scope 的兼容标签。
        kind = str(run.get("kind") or kind_for_scope(scope))
        before_row = await common.schema_io.get_row(tenant_id, kind, kb, ds)

        # ---- A3：restore **之前**落 pending 标记（崩溃窗口可恢复）。
        #      同一 CAS `adopted`：状态被并发改走时一行不写、直接 409。
        if not await store.update(
                run_id, tenant_id=tenant_id,
                rollback_target_version_id=target_id,
                rollback_from_version_id=current_id,
                rollback_started_at=common._now_utc(),
                expect_statuses={"adopted"}):
            raise ResourceConflictError(
                message=f"{run_id} 状态已变化，回滚未开始")

        # ---- 恢复目标快照（内部再校验 hash；失败不允许留半套写入）
        # 只把**内容性**失败（content 为空 / 写后 hash 不符）映射为 409；
        # 乐观锁冲突、DB 故障等必须原样上抛（500），且保留 pending 标记供
        # `recover_stale` 重试，不能伪装成「回滚恢复失败」。
        try:
            await common.schema_io.restore_snapshot(tenant_id, target)
        except common.schema_io.SnapshotRestoreError as exc:
            # 内容性失败：快照本身不可用，重试也不会变，清标记并映射 409。
            await store.clear_rollback_pending(tenant_id, run_id)
            raise ResourceConflictError(
                message=f"回滚恢复失败：{exc}") from exc
        except JonexException:
            # 确定性失败（乐观锁冲突 / 参数 / 护栏）：重试必然同样失败，
            # 清标记并原样上抛；保留 pending 只会让 sweep 反复撞同一面墙。
            await store.clear_rollback_pending(tenant_id, run_id)
            raise
        await common._stage_log(store, tenant_id, run_id, "restored",
                         started=started, version_id=target_id)

        # ---- 新建回滚版本（append-only，不翻转历史）：parent=当前 active，
        #      source=目标版本，snapshot/hash 与目标一致。
        version: dict | None = None
        try:
            version = await versions.create_candidate(
                tenant_id, kb, scope, ds, snapshot,
                parent_version_id=current_id,
                source_run_id=run_id,
                source_version_id=target_id,
                created_by=common._identity(user_id),
                note=f"rollback:{target_id}")
            if version.get("snapshot_hash") != target.get("snapshot_hash"):
                raise ResourceConflictError(
                    message="回滚版本快照 hash 与目标版本不一致")
            await common._stage_log(store, tenant_id, run_id, "version_created",
                             started=started,
                             version_id=version["version_id"])
            await versions.activate(tenant_id, version["version_id"],
                                    previous_id=current_id)
            await common._stage_log(store, tenant_id, run_id, "activated",
                             started=started,
                             version_id=version["version_id"])
        except Exception:
            # 已恢复的内容与 active 指针都退回切换前，别留半套状态。
            await common._undo_rollback_switch(tenant_id, versions,
                                        version=version, previous=current)
            await store.clear_rollback_pending(tenant_id, run_id)
            raise

        ok = await store.update(
            run_id, tenant_id=tenant_id, status="rolled-back",
            stage="rolled-back",
            rolled_back_at=common._now_utc(), rolled_back_by=common._identity(user_id),
            rollback_reason=reason or None,
            rollback_started_at=None,
            expect_statuses={"adopted"})
        if not ok:
            # 极端并发：版本已切换但 run 收口 CAS 未命中 → 全部补偿。
            await common._undo_rollback_switch(tenant_id, versions,
                                        version=version, previous=current)
            await store.clear_rollback_pending(tenant_id, run_id)
            raise ResourceConflictError(
                message=f"{run_id} 状态已变化，回滚结果未能记账")
        await common._stage_log(store, tenant_id, run_id, "rolled_back",
                         started=started, version_id=version["version_id"])
        # 值同义词：restore 已把 `ontology_synonyms` 改回目标版本，读口缓存
        # 必须失效（失败只 warning，不能推翻已成功的回滚）。
        common.bump_value_aliases(kind, tenant_id, kb)

        # ---- 台账（与采纳同 ledger_type / run_id；note=理由、版本用实际值）
        after_row = {}
        try:
            after_row = await common.schema_io.get_row(tenant_id, kind, kb, ds)
        except Exception as exc:  # noqa: BLE001 —— 只影响台账的版本号
            common.logger.warning("回滚后读 schema 版本失败 run=%s: %s", run_id, exc)
        ident = rollback_ledger_identity(kind, scope, ds, run)
        entry = {
            # relation / constraint / mapping 的回滚行沿用各自账本口径
            # （entry.kind 与采纳行同 kind）；`op="rollback"` 是回滚判据 ——
            # 采纳行没有 op="rollback"，`has_rollback_entry` 才不会把
            # 「只采纳未回滚」误判成已回滚。
            "kind": ident["entry_kind"],
            "version_id": version["version_id"],
            "target_version_id": target_id,
            "kb": kb,
            "version_before": (before_row or {}).get("schema_version"),
            "version_after": (after_row or {}).get("schema_version"),
            "verified": True,
            "at": common._now(),
            "note": reason,
        }
        if ident["op"]:
            entry["op"] = ident["op"]
        # compiled 链（含 relation / constraint）历史上一律带 ds_id；
        # mapping 的 ds_id 来自语料源，必须落键（哪怕解析不出）。
        if scope == "compiled" or ident["entry_kind"] == "mapping":
            entry["ds_id"] = ident["ds_id"]
        # I3：台账失败不能把已成功的回滚变成 500，也不能吞掉审计。
        try:
            await common.LedgerService(self.session).append(
                tenant_id, ledger_type_for_kind(kind), entry, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 —— 台账是旁路
            message = f"台账写入失败：{exc}"
            warnings.append(message)
            common.logger.warning("回滚台账写入失败 run=%s: %s", run_id, exc)
            try:
                await store.log(tenant_id, run_id, message)
            except Exception:  # noqa: BLE001 —— 日志也是旁路
                pass

        # ---- 操作审计（旁路；失败进 warnings + run_logs）
        await common._record_audit(
            store, self.session, tenant_id=tenant_id, run_id=run_id,
            action="evolution.rollback", outcome="success",
            request_params={
                "to_version_id": target_id,
                "target_legacy": legacy_target,
                "target_archived": archived_target,
                "reason": reason or None,
                "version_id": version["version_id"],
            },
            user_id=user_id, warnings=warnings)

        # ---- 归档 best-effort（失败不回滚主流程）
        try:
            await versions.archive_old(tenant_id, kb, scope, ds, keep=50)
        except Exception as exc:  # noqa: BLE001 —— 归档失败只 warning
            common.logger.warning("回滚后归档旧版本失败 run=%s: %s", run_id, exc)

        updated = await store.get(tenant_id, run_id)
        return {
            "ok": True, "rc": 0,
            "stdout_tail": f"已回滚到版本 {target_id}（新建版本 "
                           f"{version['version_id']}）",
            "run": updated,
            "version_id": version["version_id"],
            "target_version_id": target_id,
            "legacy": legacy_target,
            "archived": archived_target,
            "rolled_back": 1, "count": 1,
            "warnings": list(warnings),
            "idempotent_replay": False,
        }

    async def _rollback_replay(self, tenant_id: str, run: dict, *,
                               scope: str, kb: str, ds: str | None,
                               explicit: str | None) -> dict:
        """`rolled-back` 重放：不新建版本、不重复台账/审计。

        目标版本的优先级：成功回滚保留的 `rollback_target_version_id` →
        显式 `to_version_id` → `parent_version_id`。成功回滚后 active 已被
        回滚版本推进，`parent_version_id` 只是采纳前的旧 active，用它汇报
        会指错目标（A3 的持久化语义）。

        **回滚**台账缺失时 best-effort 补写一条（`note="replay backfill"`）；
        判据是 `entry.kind="rollback"`，不能用 run 级 `has_entries`（采纳成功
        必写 alias/query_rule 台账，会把主场景误判成已存在）。已有回滚行绝不
        重复追加。检测/补写失败只进 warnings，并回滚会话避免失败事务外溢。
        """
        target_id = (run.get("rollback_target_version_id")
                     or explicit
                     or run.get("parent_version_id"))
        versions = common.VersionStore(self.session)
        target = None
        if target_id:
            try:
                target = await versions.get(tenant_id, target_id,
                                            include_archived=True)
            except ResourceNotFoundError:
                # 版本已被归档清理是正常语义：重放仍按原结论成功返回。
                target = None
        # 其它读库异常（连接断 / SQL 错）绝不能静默降级成 legacy=false/
        # archived=false 的「成功」响应 —— 如实上抛。
        active = await versions.active_for(tenant_id, kb, scope, ds)

        warnings: list[str] = []
        ledger = common.LedgerService(self.session)
        run_id = run.get("run_id")
        try:
            has_ledger = await ledger.has_rollback_entry(tenant_id, run_id)
        except Exception as exc:  # noqa: BLE001 —— 检测失败按「未知」处理
            has_ledger = None
            common.logger.warning("回滚重放检测台账失败 run=%s: %s", run_id, exc)
            await common._rollback_session(self.session)
        if has_ledger is False:
            # 优先引用本 run 为 target 建的回滚版本；查不到再退回当前 active。
            version_id, version_id_source = None, "active"
            try:
                run_versions = await versions.list_for_run(tenant_id, run_id)
                matched = [
                    v for v in run_versions
                    if isinstance(v, dict)
                    and v.get("source_run_id") == run_id
                    and v.get("source_version_id") == target_id
                ]
                matched.sort(key=lambda v: v.get("status") != "active")
                if matched:
                    version_id = matched[0].get("version_id")
                    version_id_source = "rollback_version"
            except Exception as exc:  # noqa: BLE001 —— 版本查找是旁路
                common.logger.warning("回滚重放查找回滚版本失败 run=%s: %s",
                               run_id, exc)
                await common._rollback_session(self.session)
            if not version_id:
                active_id = (active or {}).get("version_id")
                version_id = active_id or target_id
                version_id_source = "active" if active_id else "target"
            run_kind = str(run.get("kind") or kind_for_scope(scope))
            ident = rollback_ledger_identity(run_kind, scope, ds, run)
            entry = {
                "kind": ident["entry_kind"],
                "version_id": version_id,
                "version_id_source": version_id_source,
                "target_version_id": target_id,
                "kb": kb,
                # 原台账的 before/after 已随丢失行消失；保留键位便于人工核对。
                "version_before": None,
                "version_after": None,
                "verified": True,
                "at": common._now(),
                "note": "replay backfill",
            }
            if ident["op"]:
                entry["op"] = ident["op"]
            if scope == "compiled" or ident["entry_kind"] == "mapping":
                entry["ds_id"] = ident["ds_id"]
            try:
                await ledger.append(tenant_id, ledger_type_for_kind(run_kind),
                                    entry, run_id=run_id)
                common.logger.info("回滚重放补写台账 run=%s target=%s",
                            run_id, target_id)
            except Exception as exc:  # noqa: BLE001 —— 台账是旁路
                await common._rollback_session(self.session)
                warnings.append(f"台账补写失败：{exc}")
                common.logger.warning("回滚重放补写台账失败 run=%s: %s",
                               run_id, exc)

        return {
            "ok": True, "rc": 0,
            "stdout_tail": "已回滚（幂等重放，未重复写入）",
            "run": run,
            "version_id": (active or {}).get("version_id"),
            "target_version_id": target_id,
            "legacy": bool((target or {}).get("legacy")),
            "archived": bool((target or {}).get("archived")),
            "rolled_back": 0, "count": 0,
            "warnings": warnings,
            "idempotent_replay": True,
        }
