"""平台监控 DTO：调用监控读接口（C1–C9 + M1）出参载体。

字段名与可选性已由前后端契约冻结，不得自造字段名。这里只承载结构，
不做业务校验（校验归 service）。
"""

from typing import List, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class MonitorFlags(BaseModel):
    """监控类响应公共标志位（与 CallModeMeta 同级，任何监控响应都可带）"""

    data_delayed: bool = False  # 采集链路滞后时置真；默认 false
    demo_data: bool = False  # 返回演示/桩数据时置真；默认 false
    module_error: Optional[str] = None  # null = 该模块正常；非空 = 该模块降级原因（脱敏）


class CallModeMeta(BaseModel):
    """调用监控公共模式元数据：声明本次响应用逻辑模式还是物理（降级）模式"""

    metric_mode: str  # 逻辑模式 / 物理模式；取值待确认
    mode_version: str  # 同一查询禁止混用模式
    mode_applicable_range: dict  # {from, to}，历史重算分段展示用
    recalc_status: Optional[str]  # 历史重算状态；未完成禁止跨模式同比/环比


class CallSummary(BaseModel):
    """调用监控统计卡（C1 外部依赖 / C3 开放能力）"""

    total_calls: int  # 调用总量（所选周期内逻辑调用数；降级模式改按 Attempt 数）
    success_rate: Optional[float]  # 成功率 = 成功 ÷（success + failed）；取消/执行中/未知不进分母
    avg_latency_ms: Optional[int]  # 平均响应时间（含重试与等待）= final_ended_at - logical_started_at，按毫秒取整
    failed_count: int  # 失败调用数
    mode_meta: Optional[CallModeMeta]  # 模式元数据（可内联，亦可作 mode_meta 子对象）
    flags: Optional[MonitorFlags]  # 公共标志位（可内联）
    as_of: str  # 该模块独立 as-of 时间（UTC ISO8601，反映落库时刻）


class CallObjectItem(BaseModel):
    """对象/能力列表项（C2 / C4）"""

    object_id: Optional[str] = None  # 对象标识（物理列 `monitor.call_log.target`：模型名 / 能力 ID）
    object_type: str  # 对象类型：由 `protocol` 推导（`openai` ⇒ 模型服务、`invoke` ⇒ 能力）；其余 protocol 全后端无采集写入方，实际只出这 2 类
    protocol: Optional[str]  # C4 补列：值域 = 四值枚举 `invoke`/`mcp`/`http`/`openai`（物理列 `monitor.call_log.protocol`；枚举定义见 `jonex_core/common/monitor_models.py:81-89`）（C2 为 None）
    call_count: int  # 调用量（同 CallSummary.total_calls 口径）
    success_rate: Optional[float]  # 成功率
    avg_latency_ms: Optional[int]  # 平均响应时间
    failed_count: int  # 失败数
    last_called_at: Optional[str]  # 最近调用时间（UTC ISO8601）


class CallRecordItem(BaseModel):
    """调用记录项（C6）"""

    request_id: str  # 逻辑调用唯一键（去重键，**`call_log` 内唯一**；降级模式改取 call_attempt，同一 request_id 可重复属预期）
    attempt_id: Optional[str] = None  # 本行对应的 attempt；降级模式下同 request_id 可重复，C7 寻址必须带上它（正常模式无 attempt 概念 ⇒ None）
    trace_id: Optional[str]  # 链路关联 ID
    direction: str
    trigger_type: str  # 触发方 → 物理列 `monitor.call_log.trigger_type`；值域 `user`（用户直接发起）/ `system`（系统后台批量，如入库向量化），见 `jonex_core/common/monitor_models.py:92-102`
    caller: Optional[str]  # 来源服务 / 调用方 → 物理列 `monitor.call_log.source`（勿与 protocol 混）
    target: str  # 目标对象 / 能力 → 物理列 `monitor.call_log.target`
    protocol: Optional[str]  # 开放能力页签补充：值域 = 四值枚举 `invoke`/`mcp`/`http`/`openai`（物理列 `monitor.call_log.protocol`）
    method: Optional[str]
    path: Optional[str]
    status: str  # 成功/失败/客户端取消/执行中/未知 → 物理列 `monitor.call_log.final_status`（勿与 call_attempt.status 混）
    error_code: Optional[str]
    duration_ms: Optional[int]  # final_ended_at - logical_started_at（含重试与等待），按毫秒取整
    logical_started_at: str
    final_ended_at: Optional[str]
    attempt_count: int = 0
    retry_count: int = 0  # max(attempt_count - 1, 0)


class AttemptItem(BaseModel):
    """Attempt 明细（C8 重试明细；降级模式下复用同一结构）"""

    attempt_id: str
    attempt_no: int  # 尝试序号 → 物理列 `monitor.call_attempt.attempt_no`（列名冻结为 attempt_no，勿用 attempt）
    attempt_started_at: str  # 物理列名冻结为 attempt_started_at（统一两表命名，勿用 started_at）
    attempt_ended_at: Optional[str]  # 物理列名冻结为 attempt_ended_at（勿用 ended_at）
    duration_ms: Optional[int]  # attempt_ended_at - attempt_started_at，按毫秒取整
    status: str  # → 物理列 `monitor.call_attempt.status`（与 N1 复用同一套五态，见 `jonex_core/common/monitor_models.py`）
    error_code: Optional[str]
    error_message: Optional[str]  # 脱敏失败原因；缺失显示 -
    is_final: bool
    wait_before_ms: Optional[int]  # 相邻尝试间等待时长


class CallDetail(BaseModel):
    """单次调用详情（C7）"""

    request_id: str
    method: Optional[str]  # 方法
    path: Optional[str]  # 路径
    status_code: Optional[int]  # 状态码
    error_message: Optional[str]  # 错误信息（脱敏）
    request_payload: Optional[str]  # 脱敏请求（按字段白名单脱敏；脱敏失败整段关闭）
    response_payload: Optional[str]  # 脱敏响应
    prompt_tokens: Optional[int]  # 模型调用才补：Input Token（= prompt_tokens）
    completion_tokens: Optional[int]  # 模型调用才补：Output Token（= completion_tokens）
    total_tokens: Optional[int]  # 模型调用才补：Total
    stage: Optional[int]  # Attempt 序号（= attempt_no）
    meta: Optional[dict]  # 采集元数据 / 步骤（原样回传，脱敏后）；恒 None，不与 mode_meta 复用
    mode_meta: Optional[CallModeMeta] = None  # 模式元数据（C7 无区间 ⇒ applicable_range 两端为 None）


class CallObjectDetail(BaseModel):
    """对象/能力详情（C9）"""

    object_id: str
    object_type: str
    call_count: int  # 基础指标
    success_rate: Optional[float]
    avg_latency_ms: Optional[int]
    failed_count: int
    top_sources: List[dict] = []  # 来源/调用方 TOP3（每项 {name, call_count}）
    recent_failures: List[dict] = []  # 最近失败调用（每项 {request_id, error_message, logical_started_at}）
    meta: Optional[dict] = None  # 采集元数据（如需）；恒 None，不与 mode_meta 复用
    mode_meta: Optional[CallModeMeta] = None  # 模式元数据（逻辑 / 降级口径声明）


class TrendPoint(BaseModel):
    """趋势点（C5 / T2 共用）"""

    bucket_start: str  # 桶起点（按所属族：调用族 UTC `Z`，任务族 Asia/Shanghai `+08:00`）
    # 指标值（计数/比率/耗时，随 metric 而定）。
    # ⚠️ 比率 / 均值类（success_rate / avg_duration / avg_latency）的空桶返 ``null``
    # —— 「0% 成功率」与「无数据」语义相反，禁止在服务层把 ``None`` 兜成 0。
    value: Optional[float]
    closed: bool  # 桶是否已闭合（右边界 > now() 则为 false）


class MetaFilters(BaseModel):
    """筛选元数据（M1）"""

    task_types: List[dict] = []  # 任务类型列表（每项 {code, name}）；code = task_catalog_code 取值（供筛），name = task_catalog.name 中文名（供显，⛔ 前端不写死）
    statuses: List[dict] = []  # 状态字典（含既有 enum ↔ PM 六态映射；每项 {code, label, pm_label, in_denominator}）
    objects: List[dict] = []  # 对象类型列表（每项 {object_type, protocol}）
    call_sources: List[str] = []  # 供「来源」下拉的候选值，取自 `monitor.call_log.source` 的 distinct 值


# ==================== Token 监控族（K1–K4）====================
# 数据源 = 既有表 `metering.llm_usage_log`；调用次数一律 `SUM(call_count)`（⛔ 非
# `COUNT(DISTINCT request_id)`）。时间字段类型是 `str`（UTC ISO8601），不是 datetime。


class TokenSummary(BaseModel):
    """Token 用量统计卡（K1）"""

    call_count: int  # 模型调用次数 = SUM(call_count)（embedding 聚合行已按其 call_count 计入）
    total_tokens: int  # Token 总量
    avg_tokens_per_call: Optional[float]  # 平均单次 Token；分母只算「有用量回传」的调用；分母 0 ⇒ null
    model_count: int  # 使用模型数 = COUNT(DISTINCT model)
    completeness_rate: Optional[float]  # 数据完整率 = 已回传用量调用数 ÷ 总调用数；分母 0 ⇒ null
    flags: Optional[MonitorFlags]  # 公共标志位（可内联）
    as_of: str  # 该模块独立 as-of 时间（UTC ISO8601）；空表返空串


class TokenTrendPoint(BaseModel):
    """Token 用量趋势点（K2）"""

    bucket_start: str  # 桶起点（UTC ISO8601）
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    call_count: int  # = SUM(call_count)


class TokenModelTopItem(BaseModel):
    """Token 用量模型 TOP N 项（K3）"""

    model_id: str  # 本期同取物理列 model
    total_tokens: int
    ratio: Optional[float]  # 占比 = 本模型 total_tokens ÷ 全量 total_tokens；分母 0 ⇒ null


class TokenUsageItem(BaseModel):
    """Token 用量明细项（K4，按模型聚合）"""

    model_id: str  # 本期同取物理列 model（model_id / model_name 同值）
    model_name: str
    call_count: int  # = SUM(call_count)
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_tokens_per_call: Optional[float]  # 分母只算「有用量回传」的调用；分母 0 ⇒ null
    last_called_at: Optional[str]  # 最近调用时间（UTC ISO8601）
    unmetered_count: int = 0  # 未计量调用数 = SUM(call_count) FILTER (WHERE total_tokens = 0)


# ==================== 任务监控族（T1–T4）====================
# 数据源 = 既有表 `task_center.tasks` / `task_center.subtasks`（跨 capability schema，
# 一律裸 SQL，见 `repository/monitor_task.py`）。任务族时间字段是 `str`
# （**Asia/Shanghai ISO8601，带 `+08:00`**）—— 仓储列是北京
# naive（`TIMESTAMP WITHOUT TIME ZONE`），由 service 显式换算后输出。
# 任务级终态是 `completed`/`failed`；子任务级终态是 `success`/`failed`/`timeout`。


class TaskSummary(BaseModel):
    """任务监控统计卡（T1）。"""

    total_tasks: int  # 所选周期内创建的任务数
    success_rate: Optional[float]  # 成功 ÷ 已结束；不含执行中 / 等待中；分母 0 ⇒ null
    avg_duration_ms: Optional[int]  # 已结束任务 start→end 平均时长；无行 ⇒ null
    waiting_now: int  # 当前等待（实时快照，不套区间）：COUNT WHERE status='pending'
    incomplete_count: int  # 缺失开始/结束时间的终态任务数
    flags: Optional[MonitorFlags]  # 公共标志位（data_delayed / demo_data / module_error）；承载形式与 C1/C3/K1 同形 = flags 子对象
    as_of: str  # 该模块独立 as-of 时间（Asia/Shanghai ISO8601（+08:00））；空表返空串


class FailedTaskItem(BaseModel):
    """失败任务项（T3）"""

    task_id: str
    task_name: Optional[str]
    task_type: str  # = 任务目录 code（**原始** task_catalog_code，不再归并；取值见 M1 task_types[].code）
    status: str  # 任务级终态仅 completed/failed
    error_message: Optional[str]  # 异常原因（脱敏）
    finished_at: Optional[str]  # Asia/Shanghai ISO8601（+08:00）
    # ⚠️ 接口契约里**没有** reason 字段，但前端 PmTaskFailure 需要它 ⇒ 后端补列。
    #    值 = tasks.error_code 归并到前端 5 档（ontology/compile/format/parse/detect），
    #    见 service 的 reason 归并表；无 error_code 的老数据一律 "ontology"。
    reason: str


class TaskStageItem(BaseModel):
    """执行阶段子行（T4 stages[]）"""

    stage_code: str  # = subtasks.step_code（现役 parse / compile）
    stage_name: str  # = subtasks.subtask_name（文件解析 / 知识编译）
    exec_count: int  # = 该阶段子任务行数
    running_count: int  # 子任务 status='running'
    waiting_count: int  # 子任务 status='pending'
    failed_count: int  # 子任务 status ∈ {failed, timeout}
    success_rate: Optional[float]  # 成功 ÷ 已结束；已结束 = success + failed + timeout
    avg_duration_ms: Optional[int]  # 已结束阶段 start→end 平均耗时


class TaskTypeOverview(BaseModel):
    """运行概览一行（T4）"""

    task_type: str  # = 任务目录 code（**原始** task_catalog_code，不再归并；取值见 M1 task_types[].code）
    total_count: int
    running_count: int
    waiting_count: int
    failed_count: int
    success_rate: Optional[float]
    avg_duration_ms: Optional[int]
    stages: List[TaskStageItem] = []  # 展开子表：按 step_code 对子任务分组
