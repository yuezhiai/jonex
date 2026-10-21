/**
 * 平台监控页的类型定义。页面、组件与取数层（api.ts）共用这一份。
 *
 * 分成两族：
 *  - **原始数据**（`PmTaskStats` / `PmCallRecord` 等）—— 取数层的返回形状，数值是原始数字，
 *    文案是原始码。接后端后这一层换成接口 JSON 的映射，页面拿到的还是同一族。
 *  - **视图模型**（`PmKpiCard`）—— 已翻译、已格式化的展示串。由页面在 `useMemo` 里从原始数据拼出，
 *    这样 i18n 与格式化都留在 React 侧，不会跑进取数层。
 */

/**
 * 顶部时间窗预设。
 *
 * 契约档位：任务 / 调用 / Token = 1h / 24h / 7d / 30d / 自定义。
 * 1h 仍按 `hour` 分桶（后端 `bucket` 只认 hour/day），趋势只有一个点，属预期。
 */
export type PmWindowPreset = '1h' | '24h' | '7d' | '30d';

/**
 * 自定义区间（ISO8601）。
 *
 * `preset` 固定为 `custom`，只作为 `range_preset` 回显；真正的时间真值是 `from` / `to`。
 * 用对象而不是拼接字符串，是为了让取数层与预设走同一套 `pmRange`。
 */
export interface PmCustomWindow {
  preset: 'custom';
  from: string;
  to: string;
}

export type PmWindow = PmWindowPreset | PmCustomWindow;

/** 一级页签 */
export type PmTabKey = 'task' | 'call' | 'token';

/** 调用监控的二级页签 */
export type PmCallSubKey = 'external' | 'capability' | 'records';

/* ══════════════════════ 视图模型 ══════════════════════ */

/** KPI 卡。文案与数值都已定稿，组件只负责摆放 */
export interface PmKpiCard {
  key: string;
  label: string;
  value: string;
  /** 卡片右上角角标，如「实时」 */
  badge?: string;
  /** 有值时卡片可点（如失败数下钻到已筛好的调用记录） */
  onClick?: () => void;
}

/**
 * 趋势图的一个点。
 *
 * `value` 可为 null：后端对「比率 / 均值」类指标（成功率、平均耗时）的空桶返 null ——
 * 空桶即「这一格没有任何调用」，与 0 语义相反。前端不兜 0，让它断成缺口（见 `TrendAreaChart`）。
 */
export interface PmTrendPoint {
  date: string;
  value: number | null;
  /**
   * 该桶是否已闭合。`false` = 还没走完的当前桶（右边界晚于 now），其数值仍会继续增长，
   * 图上要在 tooltip 里标成「进行中」，免得被读成「这一格就是这么多」。
   * 缺省（`undefined`）按已闭合处理 —— K2 的响应不带该字段。
   */
  closed?: boolean;
  /**
   * tooltip 里要一并展示的额外量（原始数值，键由各页自行约定）。
   * K2 一次返回 `total / prompt / completion / call_count` 四个量，图只画一个，
   * 其余三个挂在这里由 tooltip 展示，避免为此多发一次请求。
   */
  detail?: Record<string, number>;
}

/**
 * 趋势图的指标切换。任务侧四档「任务量 / 成功率 / 平均耗时 / 失败任务数」；
 * 调用侧复用同一组 value（标签换文案），所以类型放在公共区而不是各页各写一份。
 */
export type PmTrendMetric = 'volume' | 'successRate' | 'avgDuration' | 'failures';

/** 三个一级页签的共同入参（index.tsx 统一注入） */
export interface PmPaneProps {
  window: PmWindow;
  /** 刷新计数。不在页签内部使用，只进取数 useCallback 的依赖，靠它变化触发重取 */
  refreshKey: number;
  /**
   * 主取数完成时回调，工具条据此更新「数据更新于」。
   *
   * `asOf` 是后端各统计接口返回的落库时刻：它才是「数据截止到何时」的真值。
   * 传空串（如只有列表、没有统计的页签）= 没有 as-of，工具条退回本地取数时刻；
   * 不传 = 本次不涉及 as-of，保留上一份值。
   */
  onLoaded: (asOf?: string) => void;
}

/* ══════════════════════ 任务监控 ══════════════════════ */

/**
 * 任务统计卡。
 *
 * `successRate` / `avgDurationSec` 可为 null：与调用侧同理，窗口内没有「已结束」任务时后端给
 * null，此时「0%」是错误表述（没有任务结束 ≠ 全部失败），页面须显示占位符。
 * `waiting` 是**实时快照**（不套时间窗），页面「实时」角标挂在这张卡上。
 */
export interface PmTaskStats {
  total: number;
  /** 百分数（接口给 0–1 小数，映射层 ×100）；空窗口为 null */
  successRate: number | null;
  /** 秒（接口给毫秒，映射层 ÷1000）；无样本为 null */
  avgDurationSec: number | null;
  waiting: number;
  /** 降级标志。非空表示后端数据源不可用，页面据此把「降级」与「没数据」分开 */
  moduleError: string | null;
  /** 数据落库时刻（接口原样透传，任务族为 `+08:00`）；降级空壳为空串 */
  asOf: string;
}

/** 失败原因。五档只做视觉区分，不表达严重度排序 */
export type PmFailureReason = 'ontology' | 'compile' | 'format' | 'parse' | 'detect';

export interface PmTaskFailure {
  taskId: string;
  name: string;
  /** 任务目录 code；「全部任务」视图下靠它区分同一条列表里的不同类型 */
  taskType: PmTaskType;
  reason: PmFailureReason;
  /** 已格式化的时刻（平台时区），如 10-13 16:40；缺失时刻落成 '-' */
  time: string;
}

/** 任务运行概览的一行；展开后出子行（各执行阶段） */
export interface PmTaskOverviewRow {
  /**
   * 任务目录 code（`task_center.tasks.task_catalog_code`）。取值由 M1 `task_types` 下发，
   * 是**可增的目录值**（如 `file_processing_ontology`），不能写成前端枚举 ——
   * 展示名同时由 M1 的 `name` 给出。
   */
  taskType: PmTaskType;
  total: number;
  running: number;
  waiting: number;
  failed: number;
  /** 百分数；分母为 0 时为 null */
  successRate: number | null;
  /** 秒；无样本为 null */
  avgDurationSec: number | null;
  /** 展开后的阶段明细 */
  stages: PmTaskStageRow[];
}

/** 任务目录 code。不设字面量枚举：目录项由数据库维护，前端随 M1 走 */
export type PmTaskType = string;

export interface PmTaskStageRow {
  stage: PmStageKey;
  /** 阶段展示名，直接取后端 `stage_name`（比 `stage.${code}` 的 i18n 更抗新增目录） */
  stageName: string;
  running: number;
  waiting: number;
  failed: number;
  /** 百分数；分母为 0 时为 null */
  successRate: number | null;
  /** 秒；无样本为 null */
  avgDurationSec: number | null;
}

/** 执行阶段。文档处理与文件检测的阶段不同名，所以是 5 个而不是 3 个 */
export type PmStageKey = 'parse' | 'extract' | 'compile' | 'detect' | 'aggregate';

/* ══════════════════════ 调用监控 ══════════════════════ */

/** 调用方向，与后端 `CallDirection` 同取值 */
export type PmCallDirection = 'inbound' | 'outbound';

/**
 * 逻辑调用终态（五态），与后端 `CallFinalStatus` 同取值。
 *
 * **口径**：成功率的分母只含 success + failed —— 取消 / 执行中 / 未知不进分母。
 * 所以这三个不能画成失败色，否则表格里红 tag 的条数会跟统计卡的成功率对不上。
 */
export type PmCallStatus = 'success' | 'failed' | 'cancelled' | 'running' | 'unknown';

/** 调用方式 / 协议，与后端 `Protocol` 同取值 */
export type PmCallProtocol = 'invoke' | 'mcp' | 'http' | 'openai';

/** 触发方：用户直接发起 / 系统后台批量 */
export type PmCallTrigger = 'user' | 'system';

/**
 * 统计卡。
 *
 * `successRate` / `avgLatencyMs` 可为 null：窗口内一次调用都没有时，这两个值后端给 null，
 * 此时「0%」是错误表述（没有调用 ≠ 全部失败），页面须显示占位符。
 */
/**
 * 统计口径。`degraded` 表示后端按**物理 attempt**（重试各算一次）取数，
 * 不是逻辑调用 —— 调用量与耗时会显著高于直觉，页面必须把这个口径讲出来。
 */
export type PmCallMetricMode = 'logical' | 'degraded';

export interface PmCallStats {
  total: number;
  successRate: number | null;
  avgLatencyMs: number | null;
  failed: number;
  /** 降级标志。非空表示部分数据源不可用，页面据此给「降级」提示 */
  moduleError?: string | null;
  /** 本次响应的统计口径；接口缺省 `mode_meta` 时为 null */
  metricMode: PmCallMetricMode | null;
  /** 数据落库时刻（接口原样透传，UTC ISO8601）；降级空壳为空串 */
  asOf: string;
}

/**
 * 「调用对象」（外部依赖）与「开放能力」两张表的**共同形状**。
 *
 * `protocol` 在外部依赖与开放能力两个列表里都可能为 null（前者只是没有按协议筛选的入参），所以这里写成可空字段
 * 而不是拆两个类型 —— 两张表除这一列外完全同构，拆开只会多一份几乎相同的代码。
 */
export interface PmCallScopeRow {
  /** 对象 / 能力标识，来自接口的 `object_id`（`object_type` 只是分类码，不能当名字） */
  name: string;
  calls: number;
  successRate: number | null;
  avgLatencyMs: number | null;
  failed: number;
  lastCallAt: string | null;
  protocol: PmCallProtocol | null;
}

/**
 * 调用记录（C6）的筛选值。key 与 CommonTable 的 `filters[].key` 一一对应，
 * 也正是 `getCallRecords` 收的查询参数名。
 *
 * 必须是 type 别名而非 interface：CommonTable 的 `F extends Record<string, unknown>`
 * 要求隐式索引签名，TS 只给类型别名补、不给 interface 补（TS #15300，
 * 同 `pages/TaskCenter/types.ts` 的 `TaskFilters`）。
 *
 * 各字段取 `'all'`（前端「全部」档，见 `PM_ALL`）表示不筛；接口层会把它剥掉不传，
 * 所以这里写联合类型而不是可选字段 —— 筛选控件永远持有一个具体的值。
 */
export type PmCallRecordFilters = {
  direction: PmCallDirection | 'all';
  status: PmCallStatus | 'all';
  triggerType: PmCallTrigger | 'all';
  protocol: PmCallProtocol | 'all';
  source: string;
  /**
   * 目标对象 / 能力，**精确匹配**。对象详情「查看全部调用」下钻时预填 ——
   * 用 keyword 模糊匹配会把同前缀的其它对象一并带进来。
   */
  target: string;
  keyword: string;
};

export interface PmCallRecord {
  /** 已格式化的本地时刻 */
  time: string;
  direction: PmCallDirection;
  /**
   * 本行对应 attempt 的 id。降级模式下同一个 `requestId` 会出现多行，靠它区分 ——
   * 表格 `rowKey` 与 C7 寻址都用它（正常模式为 null，回落到 `requestId`；C8 不收这个参数）。
   */
  attemptId: string | null;
  triggerType: PmCallTrigger;
  /** 来源 / 调用方 */
  caller: string | null;
  /** 被调对象 / 能力 */
  target: string;
  /**
   * 调用方式。装的是接口的 `protocol` 码（不是 HTTP method）——
   * 契约 §5.1 明确「前端 method 装的就是调用方式，直接接 protocol」。
   */
  method: PmCallProtocol | null;
  status: PmCallStatus;
  latencyMs: number | null;
  /** 失败才有错误码，非失败是 null */
  errorCode: string | null;
  requestId: string;
  /** 链路关联 ID。后端可能不给（老数据 / 采集链路未打通），此时为 null */
  traceId: string | null;
}

/* ────────── C7 / C8 / C9 下钻 ────────── */

/** C7 单次调用详情 */
export interface PmCallDetail {
  requestId: string;
  method: string | null;
  path: string | null;
  statusCode: number | null;
  errorMessage: string | null;
  /** 接口侧已脱敏 */
  requestPayload: string | null;
  responsePayload: string | null;
  /** 仅模型调用有 */
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  /** = attempt 序号 */
  stage: number | null;
  /** 采集元数据 / 步骤（后端脱敏后原样回传）。本期恒为 null，有才展示 */
  meta: Record<string, unknown> | null;
  /** 本条记录按逻辑调用还是物理 attempt 取数 */
  metricMode: PmCallMetricMode | null;
}

/** C8 一次物理 attempt（重试明细的一行） */
export interface PmCallAttempt {
  attemptId: string;
  attemptNo: number;
  attemptStartedAt: string | null;
  attemptEndedAt: string | null;
  durationMs: number | null;
  status: PmCallStatus;
  errorCode: string | null;
  errorMessage: string | null;
  isFinal: boolean;
  /** 本次 attempt 开始前等待了多久 */
  waitBeforeMs: number | null;
}

/** C9 对象详情里的来源条目 */
export interface PmCallObjectSource {
  name: string;
  callCount: number;
}

/** C9 对象详情里的最近失败条目 */
export interface PmCallObjectFailure {
  requestId: string;
  errorMessage: string | null;
  time: string | null;
}

/**
 * C9 对象 / 能力详情。
 *
 * 没有 `protocol` 字段：它在 C9 里是**入参**（用来在重名对象间定位），响应体不返回，
 * 抽屉里那一格显示的是用户点进来的那一行自带的协议。
 */
export interface PmCallObjectDetail {
  objectId: string;
  objectType: string;
  callCount: number;
  successRate: number | null;
  avgLatencyMs: number | null;
  failedCount: number;
  topSources: PmCallObjectSource[];
  recentFailures: PmCallObjectFailure[];
  /** 基础指标按逻辑调用还是物理 attempt 统计 */
  metricMode: PmCallMetricMode | null;
}

/* ────────── M1 筛选元数据 ────────── */

/**
 * 下拉候选值。
 *
 * 页面**只把 M1 当首选**，取不到时回退到静态兜底（见 api.ts `getMetaFilters` 的吞异常处理）。
 * `code` 供接口筛选，`name` 供展示 —— 任务目录是库里的数据，前端不写死中文名。
 */
export interface PmMetaFilters {
  taskTypes: { code: string; name: string }[];
  /** `code` 为 null 的档位（如「超时」）只能展示，不能作为筛选参数下发 */
  statuses: { code: string | null; label: string; pmLabel: string; inDenominator: boolean }[];
  objects: { objectType: string; protocol: string | null }[];
  /** 「来源」下拉的候选 */
  callSources: string[];
}

/* ══════════════════════ Token 监控 ══════════════════════ */

export interface PmTokenStats {
  modelCalls: number;
  totalTokens: number;
  /** 空窗口下后端返 null（分母没有一次「有用量回传」的调用），不得兜成 0 */
  avgTokensPerCall: number | null;
  modelCount: number;
  /**
   * 用量回传完整率（百分数，映射层 ×100）= 已回传用量的调用数 ÷ 总调用数。
   * 分母为 0 时后端返 null：此时「0%」会被读成「全部调用都没回传」，与「没有任何调用」相反。
   *
   * 它解释的是「Token 总量为什么可能偏小」：未回传的那部分调用压根没有 token 数，
   * 平均值也把它们排除在分母之外（见 `unmeteredCount`）。
   */
  completenessRate: number | null;
  /** 降级标志。非空表示后端数据源不可用，页面据此把「降级」与「没数据」分开 */
  moduleError: string | null;
  /** 数据落库时刻（接口原样透传，UTC ISO8601）；降级空壳为空串 */
  asOf: string;
}

export interface PmModelUsageRow {
  name: string;
  modelId: string;
  calls: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** 空均值同 KPI；渲染成 '-' 而不是 0 */
  avgTokensPerCall: number | null;
  lastCallAt: string;
  /** 该模型「没有回传用量」的调用数（total_tokens = 0）。它们不进平均值分母 */
  unmeteredCount: number;
}

/** 模型使用分布的条目。percent 不存在这里，由 count 与同组总量算出（见 UsageBarList） */
export interface PmModelShareItem {
  key: string;
  label: string;
  count: number;
}
