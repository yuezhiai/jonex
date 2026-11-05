#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""② 运行时观测（运行时证据回写）—— 派生 / 记录 / 装配 / 调度写。

设计：docs/superpowers/specs/2026-10-12-nl2sql-runtime-observation-design.md（工作区文档，不入库）。
落点：独立暂存表 `knowledge_base.nl2sql_observations`（009 迁移）；诊断侧从它读失败行。

⚠️ 本模块是**旁路**：审计（`service._audit*` + `jonex_core.common.audit`）一字不动。
写失败姿态与审计 `schedule_emit` 同级：**吞异常只 warning**（观测写丢 = 该次失败从诊断
消失，与今天审计写丢同级）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from jonex_core.common.config import config
from jonex_core.common.database import get_db_session

from ...repository.nl2sql_observation_repository import Nl2SqlObservationRepository
from .schema_read import collected_drift

logger = logging.getLogger(__name__)

#: 有界性常量（设计 §4.1/§5.1）。观测是诊断输入、**有意比审计宽**：
#: question 500 / reason 500 / sql 1000（审计侧分别是 200/200/500，不动）。
MAX_RESULT_CHARS = 200
MAX_ARG_CHARS = 80
MAX_NEAR_MISS = 5
MAX_SQL_CHARS = 1000
MAX_QUESTION_CHARS = 500
MAX_REASON_CHARS = 500
#: `build_scene` 里 `first_error` 的字符上限。与 `MAX_RESULT_CHARS` 数值巧合（都是 200）、
#: 语义不同：那是工具结果的字符预算、这是失败原因首句的展示预算，各自独立可调。
MAX_FIRST_ERROR_CHARS = 200

#: `_dump`（multiturn.py）截断后缀 —— 带它说明模型也没看全（§4.3 第 1 条）。
_TRUNCATED_SUFFIX = "…(truncated)"


def summarize_tool_result(result_text: str) -> list[str]:
    """工具结果 JSON → flags（设计 §4.3 真值表；纯函数、确定性）。

    判据是 `multiturn.py` 三个工具的**结果形状**（模块内契约），不是文本猜测。
    """
    text = str(result_text or "")   # 全函数化：非 str 输入不抛 AttributeError
    if text.endswith(_TRUNCATED_SUFFIX):
        return ["truncated"]        # 已非合法 JSON，不再解析
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ["tool_error"]       # 理论不可达；到了这里就按坏结果记
    if not isinstance(data, dict):
        return ["tool_error"]
    err = data.get("error")
    if isinstance(err, str):
        return ["unknown_entity"] if err.startswith("unknown entity:") else ["tool_error"]
    note = data.get("note")
    if isinstance(note, str):
        if "is not on entity" in note:
            return ["column_absent"]
        if "no samples" in note:
            return ["no_samples"]
    if "joins" in data and data.get("joins") == []:
        return ["no_joins"]
    if "constraints" in data and data.get("constraints") == []:
        return ["no_constraints"]
    return ["ok"]


def _truncate_args(args: Any) -> dict:
    """args 进 trajectory 前过一档（§4.4）：非 dict（模型回坏 JSON）→ `{"raw": str[:80]}`；
    dict → 每值 ≤80 字符（数字/布尔/None 原样留，JSON 安全）。"""
    if not isinstance(args, dict):
        return {"raw": str(args)[:MAX_ARG_CHARS]}
    return {str(k): (v if isinstance(v, (int, float, bool)) or v is None
                     else str(v)[:MAX_ARG_CHARS])
            for k, v in args.items()}


class ToolRecorder:
    """工具调用记录器（duck-typed —— `multiturn` 只认 `record` 一个方法，不认识本类型）。

    生命周期（设计 §5.3a）：service 在 attempt 循环**外**创建、**跨尝试保留**
    （B 重试第二次的轨迹接着记）；每次生成前 `set_attempt(attempt + 1)`。
    超 `max_calls`：抛最旧、**保末 N 条**并置 `truncated`（近端上下文对诊断更有用）。
    """

    def __init__(self, max_calls: int) -> None:
        self._max = max(0, int(max_calls))
        self._calls: list[dict] = []
        self._total = 0
        self._attempt = 1

    def set_attempt(self, attempt: int) -> None:
        self._attempt = int(attempt)

    def record(self, tool: str, args: Any, result_text: str) -> None:
        self._total += 1
        self._calls.append({
            "attempt": self._attempt,
            "tool": str(tool),
            "args": _truncate_args(args),
            "flags": summarize_tool_result(result_text),
            "result": str(result_text or "")[:MAX_RESULT_CHARS],
        })
        if len(self._calls) > self._max:
            del self._calls[:len(self._calls) - self._max]

    @property
    def calls(self) -> list[dict]:
        # 副本：元素浅拷贝后，调用方连顶层键也改不到记录器；args/flags 内层为共享引用
        # （本模块 append 后不再修改它们，浅层即够）。
        return [dict(c) for c in self._calls]

    @property
    def total(self) -> int:
        return self._total               # 原始发生总数（≥ len(calls)）

    @property
    def truncated(self) -> bool:
        return self._total > len(self._calls)


def build_scene(*, multiturn: bool, anchor: str = "", filter_status: str = "",
                near_miss: list | None = None, recorder: ToolRecorder | None = None,
                guard_reason: str = "", rejection_kind: str = "",
                retried: bool = False,
                first_error: str = "", rounds: int | None = None,
                concept_fields: int = 0, concept_columns: int = 0,
                mapping_slots: int = 0,
                projection_violation: str = "",
                projection_retried: bool = False,
                exec_stats: dict | None = None,
                empty_retried: bool = False,
                value_stats: dict | None = None,
                plan_stats: dict | None = None,
                preview_stats: dict | None = None,
                format_stats: dict | None = None,
                candidate_stats: dict | None = None,
                value_alias_rewrite: list[dict] | None = None,
                hints_meta: dict | None = None,
                evolution_run: str = "",
                payload_version: str = "") -> dict:
    """把一个落点的现场装成 scene（设计 §4.1：键**全部可选、按档出现**，不用 null 填充）。

    恒有键两个：`multiturn`（bool）与 `trajectory`（单次档 = `[]`）。
    `tool_calls` / `trajectory_truncated` 仅当 recorder 存在（多轮档）。
    `concept_fields` / `concept_columns` / `mapping_slots`（语义注入计数）**仅当本次
    真注入（>0）时出现**，语义与其它可选键同款。
    `payload_version`（Task 4：生成 payload + 规则版本的指纹）**仅当非空时出现** ——
    没走到生成的早退路径与旧调用方一个键都不加。
    `exec_stats`（A3）：`{"calls","rows","errors"}`，**仅非 0 的计数落键**；没探索时
    一个键都不加（开关关 ⇒ 省略该参数，scene 与今天逐字节相同）。

    `evolution_run` 非空时打**基准标记**（三键齐落）：演化台的基准臂反复问同一批题，
    这些观测是**合成流量**，必须能整类排除出诊断/点踩读取器，否则会冲淡真实失败归因。
    `source` 用 `"evolution_bench"`（与普通检索 scene 的 source 语义区分）。
    空串 = 普通用户提问，一个键都不加（按档出现纪律）。
    """
    scene: dict = {"multiturn": bool(multiturn), "trajectory": []}
    if anchor:
        scene["anchor"] = str(anchor)
    if filter_status:
        scene["filter_status"] = str(filter_status)
    if near_miss:
        scene["near_miss"] = near_miss
    if recorder is not None:
        scene["trajectory"] = recorder.calls
        scene["tool_calls"] = recorder.total
        scene["trajectory_truncated"] = recorder.truncated
    if guard_reason:
        scene["guard_reason"] = str(guard_reason)
    # 拒绝细分（D1）：`forbidden_pair` / `deny` / `mask_position` 等。
    # 空串 = 无细分（旧行与未细分路径），键不出现（按档出现纪律）。
    if rejection_kind:
        scene["rejection_kind"] = str(rejection_kind)[:64]
    if retried:
        scene["retried"] = True
    if first_error:
        scene["first_error"] = str(first_error)[:MAX_FIRST_ERROR_CHARS]
    if rounds is not None:
        scene["rounds"] = rounds
    # 语义注入计数（TBox 落点线，设计 §5.3）：概念**组数**与**列数**（数字，不是内容）。
    # 0 = 这次没注入，键不出现（按档出现纪律，与其它可选键同款）。
    if concept_fields:
        scene["concept_fields"] = int(concept_fields)
    if concept_columns:
        scene["concept_columns"] = int(concept_columns)
    # 语义槽计数（P1-5）：本次注入的 `filter`/`aggregation`/`grain` 条数（数字，不是内容）。
    # 0 = 这次一条槽都没有，键不出现（与上面两个计数同款按档出现纪律）。
    if mapping_slots:
        scene["mapping_slots"] = int(mapping_slots)
    # payload 版本指纹（Task 4）：这次生成的可复现标识（payload + 规则版本的 sha256）。
    # 空串 = 没走到生成（早退软失败 / 旧调用方）—— 键不出现（按档出现纪律）。
    # **只进观测、不进 HTTP 响应**（与 `schema_drift` 同口径）。
    if payload_version:
        scene["payload_version"] = str(payload_version)[:64]
    # 投影契约（A1）：违规码（select_star / wide_scalar）与「是否已重写过一次」。
    # 空串 / False = 未触发，键不出现（按档出现纪律）。
    if projection_violation:
        scene["projection_violation"] = str(projection_violation)[:32]
    if projection_retried:
        scene["projection_retried"] = True
    # 执行工具计数（A3）：次数 / 回给模型的总行数 / 错误次数。0 = 这一问没探索，
    # 键不出现（按档出现纪律；开关关时恒 0 ⇒ scene 与今天逐字节相同）。
    # hints 摘要（A2）：计数 / 总字符 / 内容 hash —— **不记原文**（设计 §6）。
    if hints_meta:
        if hints_meta.get("count"):
            scene["hints_count"] = int(hints_meta["count"])
        if hints_meta.get("chars"):
            scene["hints_chars"] = int(hints_meta["chars"])
        if hints_meta.get("hash"):
            scene["hints_hash"] = str(hints_meta["hash"])[:12]
        if hints_meta.get("rewritten"):
            scene["hints_rewritten"] = True
    if empty_retried:
        scene["empty_retried"] = True
    if value_stats:
        if value_stats.get("calls"):
            scene["value_lookup_calls"] = int(value_stats["calls"])
        if value_stats.get("hits"):
            scene["value_lookup_hits"] = int(value_stats["hits"])
    if plan_stats and plan_stats.get("chars"):
        scene["plan_chars"] = int(plan_stats["chars"])
    if preview_stats and preview_stats.get("tables"):
        scene["schema_preview_tables"] = int(preview_stats["tables"])
    # P2-3：大 schema 分块选择是否生效（仅 True 落键 —— 小 schema/关档 scene 不变）。
    if preview_stats and preview_stats.get("cte_view"):
        scene["cte_view"] = True
        tables = preview_stats.get("cte_tables") or []
        if tables:
            scene["cte_tables"] = [str(t)[:128] for t in tables[:64]]
    # P2-4a：预览示例值计数（列数 / 字符数），仅非 0 落键 —— 开关关时 scene 不变。
    if preview_stats and preview_stats.get("example_cols"):
        scene["preview_example_cols"] = int(preview_stats["example_cols"])
    if preview_stats and preview_stats.get("example_chars"):
        scene["preview_example_chars"] = int(preview_stats["example_chars"])
    # P2-4b：采样行（表数 / 字符数 / 错误数），仅非 0 落键。
    if preview_stats and preview_stats.get("sample_row_tables"):
        scene["sample_row_tables"] = int(preview_stats["sample_row_tables"])
    if preview_stats and preview_stats.get("sample_row_chars"):
        scene["sample_row_chars"] = int(preview_stats["sample_row_chars"])
    if preview_stats and preview_stats.get("sample_row_errors"):
        scene["sample_row_errors"] = int(preview_stats["sample_row_errors"])
    # S1 格式预测：预测到的输出列数（仅 >0 落键；开关关/预测空时 scene 不变）。
    if format_stats and format_stats.get("cols"):
        scene["format_cols"] = int(format_stats["cols"])
    # S3 候选：只在真的多生成了一条时落 count；choice 是 1-based（1=baseline）。
    if candidate_stats and candidate_stats.get("count", 1) > 1:
        scene["candidate_count"] = int(candidate_stats["count"])
        if candidate_stats.get("choice"):
            scene["candidate_choice"] = int(candidate_stats["choice"])
    # 值同义词改写（2026-11-05）：候选召回前把问句里的 variant 换成 canonical。
    # 只在**真发生过替换**时落键（空 = 键不出现，观测与引入前逐字节相同）；
    # 原问句仍由审计 / 观测的 `question` 字段保留。
    alias_rewrites = [
        {"term": str(item.get("term") or ""),
         "canonical": str(item.get("canonical") or "")}
        for item in (value_alias_rewrite or []) if isinstance(item, dict)
    ]
    if alias_rewrites:
        scene["value_alias_rewrite"] = alias_rewrites
    if exec_stats:
        if exec_stats.get("calls"):
            scene["exec_tool_calls"] = int(exec_stats["calls"])
        if exec_stats.get("rows"):
            scene["exec_tool_rows"] = int(exec_stats["rows"])
        if exec_stats.get("errors"):
            scene["exec_tool_errors"] = int(exec_stats["errors"])
    # 演化基准标记（Task 4）：诊断/点踩读取器按 `scene->>'evolution_bench'` 整类排除。
    # run id 截 64 字符（有界性；完整 id 由 `evolution_runs` 表持有）。
    if evolution_run:
        scene["evolution_bench"] = True
        scene["evolution_run"] = str(evolution_run)[:64]
        scene["source"] = "evolution_bench"
    # 版本化读取（P1）：本次请求里 `schema_read` 记下的漂移 scope（排序）。
    # **只进观测、不进 HTTP 响应**（R1）；没有漂移**一个键都不加**（按档出现纪律）。
    drift_scopes = collected_drift()
    if drift_scopes:
        scene["schema_drift"] = True
        scene["schema_drift_scopes"] = drift_scopes
    return scene


# 飞行中写任务的强引用（对齐 audit.schedule_emit 的姿态：事件循环可能在 task 完成前
# 关掉，不留引用会被 GC 静默吞掉）。自建小份、不复用 audit 内部实现。
_pending_tasks: set["asyncio.Task[Any]"] = set()

# 后台持久事件循环（懒启动），供无运行 loop 的同步上下文复用。⚠️ **必须持久**：
# asyncpg 连接绑在创建它的 loop 上，若每次 `asyncio.run` 新建 loop，会复用全局单例
# QueuePool 里旧 loop 的残废连接 → **隔次交替失败、静默写丢**（audit 用同一招绕开此坑；
# 本修复轮把踩回来的坑照 audit 形状填平）。
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_lock = threading.Lock()


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """获取（必要时启动）后台守护事件循环（照 `audit._ensure_bg_loop` 的形状自建）。"""
    global _bg_loop
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    with _bg_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=_run, name="nl2sql-observe-loop", daemon=True).start()
        _bg_loop = loop
        return loop


def _on_task_done(task: "asyncio.Task[Any]") -> None:
    _pending_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[nl2sql-observe] 后台写任务异常（已吞）: %s", exc)


def schedule_record(entry: dict) -> None:
    """调度一行观测的写入；对调用方**零副作用**（异常全吞、绝不冒泡）。

    开关关 → 直接 return；有运行中 loop → `create_task`（强引用防 GC）；纯同步上下文
    （CLI / 测试脚本）→ 交给**进程内持久的后台 loop**（`run_coroutine_threadsafe`，
    同 `audit.schedule_emit` 的双上下文姿态；持久 = 连接池不跨 loop 残废）。
    """
    if not config.NL2SQL_OBSERVE_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_write(entry))
        _pending_tasks.add(task)
        task.add_done_callback(_on_task_done)
        return
    # 纯同步上下文：交给后台守护 loop 线程执行（调度动作本身也吞异常）。
    # ⚠️ 求值顺序：**先取 loop、再投递** —— 反过来的话 loop 起不来时协程被弃，
    # stderr 多一条 `coroutine '_write' was never awaited`（无害噪声，但不留）。
    try:
        bg = _ensure_bg_loop()
        asyncio.run_coroutine_threadsafe(_write(entry), bg)
    except Exception as exc:  # noqa: BLE001 —— docstring 的承诺：调度失败照样吞
        logger.warning("[nl2sql-observe] 观测调度失败（已吞）: %s", exc)


async def _write(entry: dict) -> None:
    """真正落库。**任何异常只 warning、绝不冒泡**（观测写丢 = 该次失败从诊断消失，
    与今天审计写丢同级 —— 审计代码注释原文承认生产侧可丢）。"""
    try:
        async with get_db_session() as session:
            await Nl2SqlObservationRepository(session).add(**entry)
    except Exception as exc:  # noqa: BLE001 —— 观测绝不冒泡
        logger.warning("[nl2sql-observe] 观测写丢（已吞）: %s", exc)


__all__ = ["ToolRecorder", "build_scene", "schedule_record", "summarize_tool_result"]
