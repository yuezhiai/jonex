# -*- coding:utf-8 -*-
"""批量采纳（演化台 P1，设计 §D3）：同 KB、每链一个 run、1..10 条。

语义（v1）：

- **同 KB**：一次批量只服务一个知识库（跨 KB 的批改需要多套权限与版本链，
  另立项）；
- **每链一个 run**：链 = `(kb, scope(kind), ds)`，`scope` 由 `locking.scope_for_kind`
  推导 —— `query_rule` / `relation` / `constraint` 共用 compiled 链（按 ds 区分），
  `alias` / `mapping` 共用 abstract 链（`ds=None`）。同链第二个 run 在**受理前**
  静态 1001 拒绝：`accept()` 的运行时 `other_adopting` 是最后一道闸，不能当作
  批量入口的校验（那会让半批已经受理、第二批才 409，结果不可解释）；
- **确认采纳**：没有 `confirm` 参数、也不做批量预览；
- **逐条收集**：单个 run 失败（`JonexException` 或意外异常）不中断其余；结果
  逐条回 `{run_id, accepted, status, code, message}`，任何人可据 `batch_id` +
  `run_id` 复算派生 request_id 去查审计。

执行复用单 run 的受理语义：每条独立会话调用 `AdoptService.accept()`（不做
第二套状态机）；真正的 apply / 冒烟 / 基准由能力层的**批量专用串行后台任务**
（`_schedule_evolution_batch_adopt`，一个任务按输入顺序逐条 `execute`，并对
尚未开始执行的 run 做心跳刷新）跑 —— 不是逐条各开一个任务：兄弟 run 并发会
让父本臂 ruler 互踩。本模块不调度（调度是 capability 的职责）。
"""
from __future__ import annotations

import logging
import uuid

from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, JonexException

from .adopt_service import AdoptService
from .locking import scope_for_kind

logger = logging.getLogger(f"{__package__}.batch_adopt")

#: 一次批量采纳的 run 上限（设计 §D3）。超过不是性能问题，而是「批量确认」
#: 的人审语义不再成立 —— 用户不可能逐条看过 10 条以上的补丁。
MAX_BATCH_RUNS = 10
#: `evolution_runs.adopt_request_id` 的列宽（派生 request_id 必须放得下）。
REQUEST_ID_MAX = 64


def batch_request_id(batch_id: str, run_id: str) -> str:
    """批量里单条 run 的幂等键：`batch:{batch_id}:{run_id}`（截断到列宽）。

    由 `batch_id` + `run_id` 纯函数推出，审计 / 前端都可用同一公式复算，
    不需要额外字段。`run_id` 形如 `run_<12 hex>`，正常远不到 64。
    """
    return f"batch:{batch_id}:{run_id}"[:REQUEST_ID_MAX]


def _normalise_run_ids(run_ids) -> list[str]:
    """`run_ids` 形状校验 + 保序去重；空/超限/非字符串一律 1001。"""
    if not isinstance(run_ids, (list, tuple)):
        raise InvalidParameterError(message="run_ids 必须是数组")
    ids: list[str] = []
    for item in run_ids:
        if not isinstance(item, str) or not item.strip():
            raise InvalidParameterError(
                message="run_ids 只能包含非空字符串")
        ids.append(item.strip())
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise InvalidParameterError(message="run_ids 不能为空")
    if len(unique) > MAX_BATCH_RUNS:
        raise InvalidParameterError(
            message=f"批量采纳一次最多 {MAX_BATCH_RUNS} 条")
    return unique


def _chain_of(run: dict) -> dict:
    kind = str(run.get("kind") or "").strip()
    try:
        scope = scope_for_kind(kind)
    except ValueError:
        # 未知 kind 是数据/参数问题，不是 500：如实 1001，并且不落半批。
        raise InvalidParameterError(message=f"未知的演化类型: {kind}") from None
    return {
        "run_id": str(run.get("run_id") or ""),
        "kb": str(run.get("kb") or ""),
        "kind": kind,
        "scope": scope,
        "ds": run.get("ds"),
    }


def validate_batch(runs: list[dict]) -> dict:
    """纯函数校验：1..10（按 run_id 去重后）、同 KB、每链至多一个 run。

    返回 `{"kb": <kb>, "chains": [{run_id, kb, kind, scope, ds}, ...]}`。
    任何不满足 → `InvalidParameterError`（1001），冲突信息必须能定位到
    具体 run 与链 —— 前端/日志据此告诉用户「哪两条撞了」。
    """
    unique: dict[str, dict] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            raise InvalidParameterError(message="run 信息必须是对象")
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise InvalidParameterError(message="run 缺少 run_id")
        unique.setdefault(run_id, run)
    items = list(unique.values())
    if not items:
        raise InvalidParameterError(message="run_ids 不能为空")
    if len(items) > MAX_BATCH_RUNS:
        raise InvalidParameterError(
            message=f"批量采纳一次最多 {MAX_BATCH_RUNS} 条")

    kbs = {str(r.get("kb") or "").strip() for r in items}
    if "" in kbs:
        raise InvalidParameterError(message="演化运行缺少知识库归属")
    if len(kbs) > 1:
        raise InvalidParameterError(
            message="批量采纳只支持同一知识库："
                    + "、".join(sorted(kbs)))

    chains: list[dict] = []
    seen: dict[tuple, dict] = {}
    for run in items:
        chain = _chain_of(run)
        key = (chain["kb"], chain["scope"], chain["ds"])
        other = seen.get(key)
        if other is not None:
            raise InvalidParameterError(
                message="同一版本链只能采纳一个 run："
                        f"{other['run_id']} 与 {chain['run_id']} 同属链 "
                        f"(kb={chain['kb']}, scope={chain['scope']}, "
                        f"ds={chain['ds'] or '-'})")
        seen[key] = chain
        chains.append(chain)

    return {"kb": next(iter(kbs)), "chains": chains}


def _normalise_override_reason(value) -> str | None:
    """与单 run 采纳同口径：None → None；字符串去空白；其它 → 1001。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidParameterError(message="override_reason 必须是字符串")
    return value.strip() or None


async def batch_adopt(tenant_id: str, run_ids: list[str], *,
                      override_reason: str | None = None,
                      adhoc_truths: list[dict] | None = None,
                      user_id: str | None = None) -> dict:
    """同 KB / 每链一个 run 的批量受理。

    步骤：`run_ids` 形状与去重 → 逐条 `RunStore.get`（租户隔离）→
    `validate_batch` 静态校验全部通过 → 按输入顺序逐条 `accept`。
    **校验先于任何受理**：混 KB / 同链 / 超限都是请求级 1001，绝不会出现
    「前半批已采纳、后半批报错」。受理阶段的单条失败（幂等冲突、空语料、
    状态已变 …）只落进该条结果，不影响其余。
    """
    ids = _normalise_run_ids(run_ids)
    batch_id = "batch_" + uuid.uuid4().hex[:12]
    override = _normalise_override_reason(override_reason)
    # ⚠️ `adopt_common` 的协作方必须走 `common.<name>` / 函数内延迟导入：
    # 在模块级 `from .adopt_common import <name>` 会把绑定复制走，测试对
    # `adopt_common.<name>` 的 monkeypatch 会静默失效（见该模块 docstring）。
    from .adopt_common import RunStore, normalize_adhoc_truths

    truths = normalize_adhoc_truths(adhoc_truths)

    async with get_db_session() as session:
        store = RunStore(session)
        runs = [await store.get(tenant_id, run_id) for run_id in ids]
    # 结构性问题（缺 run / 混 KB / 同链）在这里整体拒绝，一条都不受理。
    validate_batch(runs)

    results: list[dict] = []
    for run in runs:
        run_id = str(run["run_id"])
        request_id = batch_request_id(batch_id, run_id)
        try:
            async with get_db_session() as session:
                out = await AdoptService(session).accept(
                    tenant_id, run_id,
                    note="",
                    user_id=user_id,
                    request_id=request_id,
                    override_reason=override,
                    adhoc_truths=truths,
                )
        except JonexException as exc:
            # 预期控制流：1001/1002/1003/3006 … 如实带 code + message。
            results.append({
                "run_id": run_id, "accepted": False, "status": None,
                "code": exc.code, "message": exc.message,
                "idempotent_replay": False,
            })
            continue
        except Exception:  # noqa: BLE001 —— 单条意外异常不中断整批
            logger.exception("批量采纳单条执行失败 run=%s", run_id)
            results.append({
                "run_id": run_id, "accepted": False, "status": None,
                # 不把内部异常文本透给调用方（与 execute() 的 500 口径一致）。
                "code": None, "message": "执行失败",
                "idempotent_replay": False,
            })
            continue
        results.append({
            "run_id": run_id,
            "accepted": out.get("accepted") is True,
            "status": out.get("status"),
            "code": None,
            "message": None,
            "idempotent_replay": bool(out.get("idempotent_replay")),
        })

    accepted = sum(1 for item in results if item["accepted"])
    return {
        "batch_id": batch_id,
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "results": results,
    }
