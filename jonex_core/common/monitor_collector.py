"""MonitorCollector — 非阻塞异步调用明细写入器（monitor schema）

采集链路的**写口**：把调用明细异步落到 ``monitor.call_log``（N1）/ ``monitor.call_attempt``（N2）。

⚠️ **本文件住在共享内核（`jonex_core/common/`），三个采集方各自实例化**：Sidecar
（N1 主记录）、llm-gateway（N1 出站）、各 capability（N2 Attempt）。每个进程**各拿一份
单例 + 独立队列 + 独立连接**，直写同一个 PG 的 monitor schema；队列刻意不与审计队列共享
maxsize —— 一个卡住不牵另一个。

范式对齐 ``audit_log_sink.AuditLogSink``（``asyncio.Queue`` + 后台批量 + 满即丢 + 吞异常），
但**刻意与审计不同**的三处：

1. **独立队列实例**，不与审计队列共享 ``maxsize`` —— 一个卡住不牵另一个。
2. **满即丢，不做同步兜底**：``put_*`` 一律 ``put_nowait``，绝不 ``await``。审计的
   「ERROR/WARN 同步直写」是审计语义，监控明细无该优先级概念，且同步写会把 DB 风险引回主链路。
3. **不采 ``log_level`` 分档**（调用明细无此概念）。

硬约束：**对业务主链路的影响必须为零** —— 采集失败 / 队列满不得阻塞业务请求，
采集侧异常一律吞掉，只 ``logger.exception``。

**同键 UPSERT（后到覆盖终态）**：两表都按冲突键 ``ON CONFLICT DO UPDATE``，不再用
``DO NOTHING``。``request_id`` 相同的两次投递**是同一次逻辑调用**（典型是跨进程重试：
上传提交失败、看门狗拿同一个 ``task_id`` 补交成功），后到的那次即终态 —— DO NOTHING
会让成功结果被静默丢弃、调用永远停在先到的失败上。同批内冲突键重复另需先收敛，否则
一条 INSERT 内撞键会被 PG 整句拒绝（细节见 :meth:`MonitorCollector._sync_write`）。

``attempt_count`` 补正（实现形态取「COUNT 补正」）：每批写完后统一按
``attempt_count = GREATEST(现值, 该 request_id 的 call_attempt 行数)`` 收敛。
单调（只增不减）+ 幂等（重复投递不会重复自增）+ 顺序无关（N2 先到 / N1 后到都成立）。
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.logger import get_logger
from jonex_core.common.tenant import require_tenant

from jonex_core.common.monitor_models import (
    CallAttempt,
    CallDirection,
    CallFinalStatus,
    CallLog,
    CallTrigger,
)

logger = get_logger(__name__)

_KIND_CALL_LOG = "call_log"
_KIND_CALL_ATTEMPT = "call_attempt"

# 采集侧允许写入的列白名单（多一个键 = 整批 INSERT 报错，故先过滤）
_CALL_LOG_FIELDS = frozenset({
    "request_id", "trace_id", "tenant_id", "direction", "source", "target", "protocol",
    "trigger_type",
    "logical_started_at", "final_ended_at", "final_status", "error_code", "error_message",
    "attempt_count",
})
_CALL_ATTEMPT_FIELDS = frozenset({
    "attempt_id", "request_id", "attempt_no", "tenant_id",
    "attempt_started_at", "attempt_ended_at", "status", "error_code", "error_message",
    "is_final", "wait_before_ms",
})

# 必填字段（缺一条即丢弃该条，避免单条脏数据炸掉整批）
_CALL_LOG_REQUIRED = ("request_id", "tenant_id", "direction", "final_status")
_CALL_ATTEMPT_REQUIRED = ("request_id", "attempt_no", "tenant_id", "status")

# 采集侧**额外**拒绝的租户字面量。
#
# `unknown` 是 llm-gateway `MeteringContext.tenant_id` 的兜底默认值：调用方没带
# 租户头时会落到它身上。但统一租户校验 `require_tenant` 拦的是空串 / `default` /
# `default_tenant` / `system` / 非法字符 —— **`unknown` 不在其中、会被放行**（实测）。
# 不拦则每条「无租户」的出站调用都会以 `unknown` 之名入库，在租户维度上凭空多出
# 一个假租户。这里在采集侧单独拒收，**不改 `require_tenant`**（它是业务共享函数，
# 改它风险外溢到所有调用方）。
_ILLEGAL_TENANT_LITERALS = frozenset({"unknown"})

# error_message 落库上限（与审计 MAX_BODY_BYTES 同量级，按字符计）
_MAX_ERROR_MESSAGE_CHARS = 8000

# ``monitor.call_log.request_id`` / ``monitor.call_attempt.request_id`` 的列宽。
_MAX_REQUEST_ID_CHARS = 64

# 定长文本列的列宽守卫（入队前校验）。单条超长会让整批 INSERT 被 PG 拒绝，
# 采集侧吞异常后整批静默丢失 —— 故宁可丢弃这一条并告警，也不让它拖垮同批。
_STRING_LIMITS = {
    # 先查关联键：超长 request_id 是本单的根因
    "request_id": 64,
    "trace_id": 64,
    "attempt_id": 64,
    "tenant_id": 64,
    "source": 128,
    "target": 128,
    "protocol": 32,
    "error_code": 64,
    "direction": 16,
    "final_status": 16,
    "status": 16,
    "trigger_type": 16,
}


def make_request_id(prefix: str, *parts: str) -> str:
    """拼一个**定长**的 ``request_id``：超长时用 SHA-1 摘要收口到 64 字符。

    短串原样返回（``prefix:part1:part2...``），与既有拼串逐字一致 —— 历史 ID 不受
    影响。拼接结果超过 ``VARCHAR(64)`` 时，返回
    ``prefix:<sha1(raw) 前 N 位>``。

    **为什么摘要而不是截断**：截断会让「共享同一前缀的不同长键」退化成同一个值
    （例如批量重试的 node 名单末尾不同、时间戳/随机尾缀不同），在
    ``UNIQUE(request_id)`` / ``UNIQUE(request_id, attempt_no)`` 上互相顶掉，监控里
    凭空少一条却毫无报错。摘要保持定长、确定性（同一逻辑调用跨进程/重试得到同一值），
    且不同 raw 大概率不同。
    """
    raw = prefix + ":" + ":".join(str(part) for part in parts)
    if len(raw) <= _MAX_REQUEST_ID_CHARS:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    if len(prefix) >= _MAX_REQUEST_ID_CHARS:
        # 前缀本身已占满列宽（理论上不该发生；当前四个前缀最长 17）：
        # 退回无前缀的纯摘要，**不抛异常** —— 采集链路绝不能影响业务主链路。
        return digest[:_MAX_REQUEST_ID_CHARS]
    return f"{prefix}:{digest[:_MAX_REQUEST_ID_CHARS - len(prefix) - 1]}"


# 文本级脱敏：`password=xxx` / `token: xxx` 形态。键名与审计 SENSITIVE_KEYS 同集合。
#
# ⚠️ 为什么分两条：
# 1. ``Authorization: Bearer <token>`` 若走通用「键=值」替换，``(\S+)`` 只吃掉
#    ``Bearer``，token 原样留在后面（旧实现的真实缺陷）。故先单独处理带 scheme
#    的凭证头，保留 scheme、只掩 token。
# 2. ``x-api-key`` / ``x_api_key`` / ``api-key`` 这类带连字符的键名不在通用键
#    集合里（``\bapi_key\b`` 也匹配不到 ``x-api-key`` 的完整前缀），必须显式列出。
#
# 值支持 ``"..."`` / ``'...'`` 引号形式（整体替换，引号一并掩掉）。**先脱敏后截断**
# —— 反过来会把跨边界的 secret 前几位留在库里。
_AUTH_SCHEME_PATTERN = re.compile(
    r"(?i)\b(authorization)(\s*[=:]\s*)(bearer|basic|digest|token)\s+[^\s,'\"]+"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
# 无 scheme 的 ``authorization=xxx``；有 scheme 的形式已被上面那条处理，
# 这里用负向断言避免把 ``Authorization: Bearer ***`` 二次改写成 ``*** ***``。
_AUTH_PLAIN_PATTERN = re.compile(
    r"(?i)\b(authorization)(\s*[=:]\s*)"
    r"(?!(?:bearer|basic|digest|token)\b)(\"[^\"]*\"|'[^']*'|\S+)"
)

_SECRET_INLINE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token"
    r"|authorization_code|secret|api[_-]?key|apikey|private_key|ticket"
    r"|x[_-]api[_-]?key)\b(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)


def _sanitize_error_message(message: Optional[Any]) -> Optional[str]:
    """错误信息脱敏 + 截断（**先脱敏、后截断**）。

    **脱敏失败 ⇒ 整段关闭**，不做部分展示 —— 拿不准就不写。
    """
    if message is None:
        return None
    try:
        text_value = str(message)
        text_value = _AUTH_SCHEME_PATTERN.sub(r"\1\2\3 ***", text_value)
        text_value = _BEARER_TOKEN_PATTERN.sub(r"\1 ***", text_value)
        text_value = _AUTH_PLAIN_PATTERN.sub(r"\1\2***", text_value)
        text_value = _SECRET_INLINE_PATTERN.sub(r"\1\2***", text_value)
        if len(text_value) > _MAX_ERROR_MESSAGE_CHARS:
            text_value = text_value[:_MAX_ERROR_MESSAGE_CHARS]
        return text_value
    except Exception:
        logger.warning("监控采集：error_message 脱敏失败，整段丢弃")
        return None


def classify_trigger_by_scene(scene: Optional[Any]) -> str:
    """按「场景标签」判定触发方：命中后台场景表 ⇒ ``system``，其余 ⇒ ``user``。

    调用方不必自己声明是用户发起还是后台发起 —— 平台每次调大模型都带
    ``X-Jonex-Scene``（如 ``ontology_embed`` 文档向量化、``ontology_qa`` 问答检索），
    采集侧照标签表归类即可。标签表在 ``MONITOR_SYSTEM_SCENES``，新场景自行补登记。
    """
    text_value = str(scene or "").strip()
    if not text_value:
        return CallTrigger.USER.value
    system_scenes = {
        s.strip() for s in (get_config().MONITOR_SYSTEM_SCENES or "").split(",") if s.strip()
    }
    return CallTrigger.SYSTEM.value if text_value in system_scenes else CallTrigger.USER.value


def _normalize(kind: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """白名单过滤 + 必填校验 + 租户合法性校验 + 脱敏。返回 None 表示该条应丢弃。"""
    allowed = _CALL_LOG_FIELDS if kind == _KIND_CALL_LOG else _CALL_ATTEMPT_FIELDS
    required = _CALL_LOG_REQUIRED if kind == _KIND_CALL_LOG else _CALL_ATTEMPT_REQUIRED

    row = {k: v for k, v in (fields or {}).items() if k in allowed}

    missing = [k for k in required if row.get(k) in (None, "")]
    if missing:
        logger.warning("监控采集：%s 缺必填字段 %s，丢弃一条", kind, missing)
        return None

    # tenant_id 存储必填且必须合法，非法**不写**（两表均无 None 兜底位）
    if str(row["tenant_id"]).strip().lower() in _ILLEGAL_TENANT_LITERALS:
        # `unknown` 等「像租户但不是租户」的兜底字面量，`require_tenant` 会放行，
        # 故在采集侧单独拒收（否则租户维度会多出一个假租户）。
        logger.warning("监控采集：%s 租户为兜底字面量，丢弃一条", kind)
        return None
    try:
        row["tenant_id"] = require_tenant(row["tenant_id"])
    except Exception:
        logger.warning("监控采集：%s 租户非法，丢弃一条", kind)
        return None

    if kind == _KIND_CALL_LOG and not row.get("trigger_type"):
        # 兜底 user（**不进必填集**）：漏标只是分类不准，不该让整条 N1 静默消失
        row["trigger_type"] = CallTrigger.USER.value

    if kind == _KIND_CALL_ATTEMPT and not row.get("attempt_id"):
        # attempt_id 后台生成（uuid4 hex）
        row["attempt_id"] = uuid.uuid4().hex

    if "error_message" in row:
        row["error_message"] = _sanitize_error_message(row["error_message"])

    # 定长列长度守卫：超长值会让**整批** INSERT 被 PG 拒绝，进而整批静默丢失
    # （采集侧只吞异常）。故在入队前丢弃这一条并告警，**不做静默截断** ——
    # 截断后的 ID 会与别的调用撞 UNIQUE，数据看起来还在、实际已经错了。
    for name, limit in _STRING_LIMITS.items():
        value = row.get(name)
        if value is None:
            continue
        length = len(str(value))
        if length > limit:
            logger.warning(
                "监控采集：%s 字段 %s 超长（%d > %d），丢弃一条",
                kind,
                name,
                length,
                limit,
            )
            return None

    return row


# UPSERT 的 SET 子句要用到的列。多行 VALUES 的列清单取自**首行**的键，某列若只出现在
# 靠后的行里就会踩 SQLAlchemy 的多值 INSERT 限制（报错或静默丢值）—— 故落库前按批补齐。
# 只列**可空且无服务端默认值**的列：这类列补 None 等价于「这行本就没给该字段」。
_CALL_LOG_UPSERT_COLUMNS = ("final_ended_at", "error_code", "error_message")
_CALL_ATTEMPT_UPSERT_COLUMNS = (
    "attempt_started_at",
    "attempt_ended_at",
    "error_code",
    "error_message",
    "wait_before_ms",
)


def _fill_missing(rows: List[Dict[str, Any]], columns: Tuple[str, ...]) -> None:
    """就地把 ``columns`` 里缺的键补成 ``None``。

    ⛔ 不要拿它补带默认值的列（``trigger_type`` / ``attempt_count`` / ``is_final`` 等）：
    显式 None 会顶掉服务端默认值、甚至撞 NOT NULL。
    """
    for row in rows:
        for column in columns:
            row.setdefault(column, None)


def _dedupe_by_conflict_key(
    rows: List[Dict[str, Any]], keys: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """同批内冲突键重复 ⇒ 只留**最后一条**（与 UPSERT「后到覆盖」同语义）。

    ⛔ **不能交给 DB 兜底**：同一条 INSERT 里同一行被 propose 两次时，PG 直接报
    ``ON CONFLICT DO UPDATE command cannot affect row a second time``，**整条语句**
    失败 —— 采集侧只吞异常、不重试（见 :meth:`MonitorCollector._sync_write`），
    整批（上限 ``MONITOR_FLUSH_BATCH_SIZE``）都会丢。``DO NOTHING`` 时代没有这个风险
    （重复即静默跳过），是改 UPSERT 带进来的，故必须在这里先收掉。

    冲突键重复**不是脏数据**，而是「同一次逻辑调用被投递了两次」：典型是 llm-gateway
    的 ``request_id`` 在缺 trace 时退化为**纯 body 哈希**（``derive_request_id``），
    同 body 同 scene 的两次并发调用会逐字撞键、且都记 ``attempt_no=1``。留最后一条
    与 UPSERT 的「后到即终态」口径一致。
    """
    if len(rows) < 2:
        return rows
    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        # dict 保序：重复键覆盖值但保留首次出现的位置，故顺序仍等于「首次投递顺序」。
        deduped[tuple(row.get(k) for k in keys)] = row
    return list(deduped.values())


class MonitorCollector:
    """调用明细异步写入器（单例，见 ``get_monitor_collector()``）。

    用法::

        collector = get_monitor_collector()
        collector.start()
        collector.put_call_log({...})
        collector.put_call_attempt({...})
        await collector.stop()   # flush 剩余后退出
    """

    def __init__(self):
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 可观测计数（静默丢数据是最坏形态）
        self.dropped_count = 0
        self.written_count = 0
        # 未启动 / 开关关闭导致的丢弃：以前是纯静默 return，排障时无从判断
        # 「没数据」到底是没调用还是采集没开。两者分开计数，各只告警一次。
        self.unstarted_count = 0
        self.disabled_count = 0

    # ---- 生命周期 ----

    def start(self):
        """启动后台消费协程（platform 服务 startup 时调用）"""
        if self._running:
            return
        config = get_config()
        self._queue = asyncio.Queue(maxsize=config.MONITOR_QUEUE_MAX_SIZE)
        self._running = True
        self._task = asyncio.create_task(self._consumer_loop())
        if not config.MONITOR_COLLECT_ENABLED:
            # 开关关闭时进程照样起来；不显式说一声，运维只会看到「监控没数据」。
            logger.warning(
                "MonitorCollector 已启动，但 MONITOR_COLLECT_ENABLED=false —— "
                "采集已关闭，所有投递都会计入 disabled_count 后丢弃"
            )
        else:
            logger.info("MonitorCollector 已启动")

    async def stop(self):
        """停止后台消费协程，flush 剩余队列后退出"""
        if not self._running:
            return
        self._running = False
        if self._queue is not None:
            await self._queue.put(None)  # 哨兵
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("MonitorCollector 停止超时，强制退出")
            self._task = None
        logger.info("MonitorCollector 已停止")

    # ---- 投递（纯非阻塞）----

    def put_call_log(self, fields: Dict[str, Any]):
        """投递一条 N1 逻辑调用。"""
        self._put(_KIND_CALL_LOG, fields)

    def put_call_attempt(self, fields: Dict[str, Any]):
        """投递一条 N2 物理 Attempt。"""
        self._put(_KIND_CALL_ATTEMPT, fields)

    def stats(self) -> Dict[str, Any]:
        """排障快照：进程内计数 + 队列水位。只读、无副作用。"""
        return {
            "running": self._running,
            "queue_size": self._queue.qsize() if self._queue is not None else 0,
            "queue_maxsize": self._queue.maxsize if self._queue is not None else 0,
            "dropped_count": self.dropped_count,
            "written_count": self.written_count,
            "unstarted_count": self.unstarted_count,
            "disabled_count": self.disabled_count,
        }

    def _put(self, kind: str, fields: Dict[str, Any]):
        if self._queue is None or not self._running:
            # 以前这里直接 return：调用方完全看不到「采集器没启动」。
            self.unstarted_count += 1
            if self.unstarted_count == 1:  # 限流：首次一条，避免每次投递刷日志
                logger.warning(
                    "MonitorCollector 未启动，监控采集被丢弃"
                    "（累计 %d 条；此告警仅首次输出，后续看 unstarted_count）",
                    self.unstarted_count,
                )
            return
        if not get_config().MONITOR_COLLECT_ENABLED:
            self.disabled_count += 1
            if self.disabled_count == 1:
                logger.warning(
                    "MONITOR_COLLECT_ENABLED=false，监控采集被丢弃"
                    "（累计 %d 条；此告警仅首次输出，后续看 disabled_count）",
                    self.disabled_count,
                )
            return
        row = _normalize(kind, fields)
        if row is None:
            self.dropped_count += 1
            return
        try:
            self._queue.put_nowait((kind, row))
        except asyncio.QueueFull:
            # 满即丢 + 计数 + 告警，绝不 await（保持业务主链路零阻塞）
            self.dropped_count += 1
            if self.dropped_count % 100 == 1:  # 限流告警噪声
                logger.warning(
                    "监控采集队列满，累计丢弃 %d 条（监控自身不可静默丢数据）",
                    self.dropped_count,
                )

    # ---- 后台消费 ----

    async def _consumer_loop(self):
        config = get_config()
        batch_size = config.MONITOR_FLUSH_BATCH_SIZE
        interval = config.MONITOR_FLUSH_INTERVAL_MS / 1000.0

        while self._running:
            try:
                await self._flush_once(batch_size)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MonitorCollector 消费异常")
            await asyncio.sleep(interval)

        await self._flush_remaining()

    async def _flush_once(self, batch_size: int):
        if self._queue is None:
            return
        batch: List[Tuple[str, Dict[str, Any]]] = []
        while len(batch) < batch_size:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:  # 哨兵
                self._running = False
                break
            batch.append(item)
        if not batch:
            return
        await self._sync_write(batch)

    async def _flush_remaining(self):
        if self._queue is None:
            return
        remaining: List[Tuple[str, Dict[str, Any]]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                remaining.append(item)
        if remaining:
            await self._sync_write(remaining)

    # ---- 落库 ----

    async def _sync_write(self, batch: List[Tuple[str, Dict[str, Any]]]):
        """批量落库：同键 UPSERT（后到覆盖终态）+ attempt_count COUNT 补正。

        **为什么是同键 UPSERT，而不是 ON CONFLICT DO NOTHING**：``request_id``
        相同的两条投递**是同一次逻辑调用**。典型场景是**跨进程重试** —— 上传时向
        任务中心提交失败、随后由看门狗拿同一个 ``task_id`` 补交成功，两条路拼出的
        ``request_id`` 逐字相同。DO NOTHING 会把后到的成功结果**静默丢弃**，这条调用
        就永远停在先到的那次失败上，监控里显示「失败」而任务其实成了。

        DO UPDATE 之后，「最后一次投递的结局」即该逻辑调用的终态 —— 与 N1「一次逻辑
        调用一条记录、记最终结局」的设计一致；进程内重试循环也就不再必须靠调用方
        把投递挪到循环外来规避（挪出去仍是好习惯，但不再是正确性的前提）。

        **顺序假设**：同一次逻辑调用的多次投递严格先后发生（重试带退避），故后到即
        覆盖。刻意不做时间戳比较 —— 跨服务时钟不可比，比出来的顺序还不如「谁后到谁赢」可靠。

        N2 的冲突键取 ``(request_id, attempt_no)``：重试后的那次投递会复用同一个
        ``attempt_no``（新 ``attempt_id``），据此把那行的结局一并刷新。

        **同批同键先去重（留最后一条）**：一条 INSERT 内撞同一个冲突键会被 PG 整句
        拒绝（``cannot affect row a second time``），故落库前先按
        :func:`_dedupe_by_conflict_key` 收敛；语义与 UPSERT 的「后到覆盖」一致。
        """
        if not batch:
            return
        # 同批同键先去重（留最后一条）：否则一条 INSERT 内撞键会被 PG 整句拒绝。
        logs = _dedupe_by_conflict_key(
            [row for kind, row in batch if kind == _KIND_CALL_LOG], ("request_id",)
        )
        attempts = _dedupe_by_conflict_key(
            [row for kind, row in batch if kind == _KIND_CALL_ATTEMPT],
            ("request_id", "attempt_no"),
        )
        # 多行 VALUES 的列清单由**首行**的键决定：某列若只在后面的行里出现，SQLAlchemy
        # 要么报 CompileError、要么把该值静默丢掉。这里先把 SET / 冲突判定要用到的列
        # 在每行上补齐（缺的填 None），列清单就与行序无关了。补的都是可空且无默认值的列。
        _fill_missing(logs, _CALL_LOG_UPSERT_COLUMNS)
        _fill_missing(attempts, _CALL_ATTEMPT_UPSERT_COLUMNS)
        try:
            await self._write_rows(logs, attempts)
            self.written_count += len(batch)
            return
        except Exception:
            # 吞异常、不回滚业务；批量失败后进入单条隔离，坏行只丢自己。
            logger.exception(
                "监控采集批量写入失败，进入单条隔离（共 %d 条）", len(batch)
            )

        # —— 隔离模式：仅在批量失败时发生，正常路径零额外开销 ——
        # 逐行独立写：一条脏数据（如超长仍未拦住的值）不该拖垮同批其余记录。
        written_request_ids = set()
        for row in logs:
            try:
                await self._write_rows([row], [], update_counts=False)
            except Exception:
                logger.exception(
                    "监控采集：单条 N1 写入失败，request_id=%s",
                    row.get("request_id"),
                )
                self.dropped_count += 1
            else:
                self.written_count += 1
                written_request_ids.add(row["request_id"])
        for row in attempts:
            try:
                await self._write_rows([], [row], update_counts=False)
            except Exception:
                logger.exception(
                    "监控采集：单条 N2 写入失败，request_id=%s attempt_id=%s",
                    row.get("request_id"),
                    row.get("attempt_id"),
                )
                self.dropped_count += 1
            else:
                self.written_count += 1
                written_request_ids.add(row["request_id"])

        # 隔离结束后统一补正一次（复用正常路径的 SQL），不给每行各跑一遍。
        try:
            await self._update_attempt_counts(written_request_ids)
        except Exception:
            logger.exception("监控采集：隔离模式 attempt_count 补正失败")

    async def _write_rows(
        self,
        logs: List[Dict[str, Any]],
        attempts: List[Dict[str, Any]],
        *,
        update_counts: bool = True,
    ) -> None:
        """单个 session 内写 N1/N2，并（默认）跑 ``attempt_count`` COUNT 补正。

        ``update_counts=False`` 供隔离模式使用：逐行写时先不补正，全部写完后再统一
        跑一次 :meth:`_update_attempt_counts`。
        """
        async with get_db_session() as session:
            if logs:
                log_stmt = pg_insert(CallLog).values(logs)
                await session.execute(
                    log_stmt.on_conflict_do_update(
                        index_elements=["request_id"],
                        set_={
                            # 终态三件套 + 结束时刻：新一次投递的结局即该逻辑调用的结局。
                            # 成功那次的 error_* 是 None，正好把上一轮的报错清掉。
                            "final_status": log_stmt.excluded.final_status,
                            "final_ended_at": log_stmt.excluded.final_ended_at,
                            "error_code": log_stmt.excluded.error_code,
                            "error_message": log_stmt.excluded.error_message,
                            "updated_at": sa_text("NOW()"),
                        },
                    )
                )
            if attempts:
                attempt_stmt = pg_insert(CallAttempt).values(attempts)
                await session.execute(
                    attempt_stmt.on_conflict_do_update(
                        index_elements=["request_id", "attempt_no"],
                        set_={
                            # attempt_id 一并换成新那次的：该行从此完整描述
                            # 「以这个结局收场的那次物理尝试」，不留下
                            # 「id 属于失败那次、状态却是成功」的自相矛盾。
                            "attempt_id": attempt_stmt.excluded.attempt_id,
                            "attempt_started_at": attempt_stmt.excluded.attempt_started_at,
                            "attempt_ended_at": attempt_stmt.excluded.attempt_ended_at,
                            "status": attempt_stmt.excluded.status,
                            "error_code": attempt_stmt.excluded.error_code,
                            "error_message": attempt_stmt.excluded.error_message,
                            "is_final": attempt_stmt.excluded.is_final,
                            "wait_before_ms": attempt_stmt.excluded.wait_before_ms,
                            "updated_at": sa_text("NOW()"),
                        },
                    )
                )
            if update_counts:
                await self._update_attempt_counts(
                    {r["request_id"] for r in logs}
                    | {r["request_id"] for r in attempts},
                    session=session,
                )

    async def _update_attempt_counts(
        self, request_ids: set, *, session: Any = None
    ) -> None:
        """按 ``request_id`` 把 N1 的 ``attempt_count`` COUNT 补正到 N2 行数。

        单调（只增不减）+ 幂等（重复投递不会重复自增）+ 顺序无关（N2 先到 / N1
        后到都成立）。隔离模式传自己的 session；否则自开一个。
        """
        if not request_ids:
            return
        # COUNT 补正 SQL（与正常路径共用一份，避免两处分叉）
        stmt = sa_text(
            "UPDATE monitor.call_log AS cl "
            "SET attempt_count = GREATEST(cl.attempt_count, sub.cnt), "
            "    updated_at = NOW() "
            "FROM ("
            "    SELECT request_id, COUNT(*) AS cnt "
            "    FROM monitor.call_attempt "
            "    WHERE request_id IN :ids "
            "    GROUP BY request_id"
            ") AS sub "
            "WHERE cl.request_id = sub.request_id "
            "  AND cl.attempt_count < sub.cnt"
        ).bindparams(bindparam("ids", expanding=True))
        params = {"ids": sorted(request_ids)}
        if session is not None:
            await session.execute(stmt, params)
            return
        async with get_db_session() as own_session:
            await own_session.execute(stmt, params)


# ---- 全局单例 ----

_collector: Optional[MonitorCollector] = None


def get_monitor_collector() -> MonitorCollector:
    global _collector
    if _collector is None:
        _collector = MonitorCollector()
    return _collector


# ---- N2 埋点：冻结接口（各重试循环共用）----


class AttemptSpan:
    """一次物理 Attempt 的采集句柄（由 :func:`attempt_span` 产出）。

    **为什么要有这层**：N2 的采集点散在十几个重试循环里（api_gateway 四个路由、
    llm-gateway 上游 429 两处、任务派活重试、task_runtime 四方向）。若各处自己
    「掐表 + 拼 dict + 兜异常」，字段含义会随人漂移。这里把那段**只写一份**，
    各采集点只负责「告诉它这次成没成」。

    用法（**默认成功**，失败由调用方显式标注）::

        with attempt_span(request_id=rid, tenant_id=tid,
                          attempt_no=n, is_final=last) as att:
            try:
                resp = await call()
            except httpx.TransportError as exc:
                att.fail(error_code=502, error_message=exc)
                raise
            if resp.status_code >= 400:
                att.fail(error_code=resp.status_code)

    - **不吞业务异常**：异常照常向上抛，本层只管记录；
    - 调用方没标注、却真抛了异常 ⇒ 兜底记 ``failed``（带 `str(exc)`）；
      ``asyncio.CancelledError`` ⇒ 记 ``cancelled``（「问了没反应」时要能分清
      「没发出去」与「发出去被打断」，故中途取消也要留痕）；
    - 录投递（队列满 / 采集器未启动 / 拼字段意外）**一律吞掉**，不影响业务主链路。
    """

    __slots__ = ("_row", "_marked", "_sealed")

    def __init__(
        self,
        *,
        request_id: str,
        tenant_id: str,
        attempt_no: int,
        is_final: bool,
        wait_before_ms: Optional[int] = None,
    ):
        self._row: Dict[str, Any] = {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "attempt_no": attempt_no,
            "is_final": bool(is_final),
            "wait_before_ms": wait_before_ms,
            "status": CallFinalStatus.SUCCESS.value,
            # DDL 是 TIMESTAMPTZ ⇒ 必须 tz-aware（朴素值会踩 asyncpg 编解码坑）
            "attempt_started_at": datetime.now(timezone.utc),
        }
        self._marked = False
        self._sealed = False

    # ---- 调用方标注结局 ----

    def fail(self, error_code: Optional[Any] = None, error_message: Optional[Any] = None) -> None:
        self._mark(CallFinalStatus.FAILED.value, error_code, error_message)

    def cancel(self, error_code: Optional[Any] = None, error_message: Optional[Any] = None) -> None:
        self._mark(CallFinalStatus.CANCELLED.value, error_code, error_message)

    def mark(
        self,
        status: Any,
        error_code: Optional[Any] = None,
        error_message: Optional[Any] = None,
    ) -> None:
        """显式标注任意状态。传 :class:`CallFinalStatus` 成员或裸字符串均可。"""
        self._mark(
            status.value if isinstance(status, CallFinalStatus) else str(status),
            error_code,
            error_message,
        )

    def mark_final(self, is_final: bool = True) -> None:
        """就地改写 `is_final`。

        构造时只能按「还剩几次重试」预判，但**成功的那次本身就是最后一次尝试** ——
        这个结论要拿到回包才知道。故给一个显式入口，避免调用方去动内部字典。
        """
        self._row["is_final"] = bool(is_final)

    @property
    def status(self) -> str:
        return self._row["status"]

    def _mark(self, status: str, error_code: Optional[Any], error_message: Optional[Any]) -> None:
        self._row["status"] = status
        self._marked = True
        if error_code is not None:
            self._row["error_code"] = str(error_code)  # 列是 String(64)
        if error_message is not None:
            self._row["error_message"] = error_message

    def _seal(self) -> Dict[str, Any]:
        """补上结束时刻并返回一份快照（只封一次，重复调用返回同一份）。"""
        if not self._sealed:
            self._sealed = True
            self._row["attempt_ended_at"] = datetime.now(timezone.utc)
        return dict(self._row)


def _emit_attempt(span: AttemptSpan) -> None:
    """投递一条 N2，**吞掉一切异常**（采集是旁路，投递失败不得影响业务）。"""
    try:
        get_monitor_collector().put_call_attempt(span._seal())
    except Exception:
        logger.exception("监控采集：N2 Attempt 投递失败（已吞异常，不影响调用）")


@contextlib.contextmanager
def attempt_span(**kwargs: Any):
    """N2 采集的**冻结接口** —— 语义与用法见 :class:`AttemptSpan`。"""
    span = AttemptSpan(**kwargs)
    try:
        yield span
    except asyncio.CancelledError:
        if not span._marked:
            span.cancel()
        raise
    except BaseException as exc:
        if not span._marked:
            span.fail(error_message=exc)
        raise
    finally:
        _emit_attempt(span)


# ---- N1 补发入口（N2 先行、N1 后补的采集点用）----


def emit_logical_call(
    *,
    request_id: str,
    tenant_id: str,
    status: Any,
    direction: str = CallDirection.OUTBOUND.value,
    source: Optional[str] = None,
    target: Optional[str] = None,
    protocol: Optional[str] = None,
    trigger_type: str = CallTrigger.SYSTEM.value,
    started_at: Optional[datetime] = None,
    error_code: Optional[Any] = None,
    error_message: Optional[Any] = None,
) -> None:
    """投递一条 N1 逻辑调用（``monitor.call_log``），**吞掉一切异常**。

    **给哪类采集点用**：先落 N2、再补 N1 的调用 —— 典型是任务运行时的重发循环。
    N2 是 per-attempt 的（:func:`attempt_span` 每次尝试发一条），而 N1 是 per-逻辑调用的，
    **只有等循环收工才知道最终结局**。这类点位原先只采 N2，于是平台里压根没有以
    ``request_id`` 建过 N1 ⇒ ``attempt_count`` 的 COUNT 补正（:meth:`MonitorCollector._sync_write`
    按 ``request_id`` 相等回填两表行数）永远补不上，``retry_count`` 恒为 0。

    ✅ **建议把它放在重试循环之外**（``finally`` 里最稳），但**已不是正确性前提**：
    N1 的冲突动作是 UPSERT（后到覆盖终态，见 :meth:`MonitorCollector._sync_write`），
    循环内逐次投递也只会留下**最后一次**的结局。挪到循环外仍值当：少几次无用写，且不会
    把中间那次失败（随即重试成功）先写成终态、再被覆盖 —— 中途读到的人会短暂误判。

    **`request_id` 必须与同一次逻辑调用的 N2 逐字相同**（两表靠它关联），``tenant_id``
    取合法租户 —— 非法 / 兜底字面量（``unknown`` 等）由采集侧整条丢弃，**不造假值兜底**。

    取值约定（⚠️ **枚举取值仍待评审**）：

    - ``direction`` 默认 ``outbound``：这些点位记的都是「本进程**发出**的调用」；
    - ``trigger_type`` 默认 ``system``：重发循环拿不到调用者身份，按「系统发起」记。
      **与 `_normalize` 的兜底值 ``user`` 刻意不同** —— 漏标时兜底 ``user`` 只是分类不准，
      而这里的调用确实不是用户直接点的；
    - ``source`` / ``target`` / ``protocol`` 选填，由调用点按自己知道的信息给。

    与 :func:`attempt_span` 的分工：那个管**每一次物理尝试**，这个管**一次逻辑调用的结局**。
    """
    try:
        ended = datetime.now(timezone.utc)
        get_monitor_collector().put_call_log({
            "request_id": request_id,
            "trace_id": request_id,  # 现与 request_id 同源，是否分离待定（同 Sidecar 侧）
            "tenant_id": tenant_id,
            "direction": direction,
            "source": source,
            "target": target,
            "protocol": protocol,
            "trigger_type": trigger_type,
            # DDL 是 TIMESTAMPTZ ⇒ 必须 tz-aware（朴素值会踩 asyncpg 编解码坑）
            "logical_started_at": started_at or ended,
            "final_ended_at": ended,
            "final_status": (
                status.value if isinstance(status, CallFinalStatus) else str(status)
            ),
            "error_code": str(error_code) if error_code is not None else None,
            "error_message": error_message,
        })
    except Exception:
        logger.exception("监控采集：N1 逻辑调用投递失败（已吞异常，不影响调用）")


__all__ = [
    "MonitorCollector",
    "AttemptSpan",
    "attempt_span",
    "emit_logical_call",
    "get_monitor_collector",
    "classify_trigger_by_scene",
    "make_request_id",
]
