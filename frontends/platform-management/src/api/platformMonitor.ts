/**
 * 平台监控页的取数入口 —— **接后端时唯一要改的文件**。
 *
 * **调用监控（C1–C9 + M1）与任务监控（T1–T4）已接真实接口**：函数签名保留，函数体换成
 * `apiClient.get`，「接口字段 → types.ts」的映射全部收在本文件，页面与组件只认 types.ts。
 * `apiClient` 会自动解包响应外壳里的 `data`，`code != 0` 时抛 `ApiError`，
 * 所以这里拿到的就是接口的业务数据，不需要判 `success`。
 *
 * ⚠️ **量纲只在本层处理**：接口给比率是 0–1 小数、时长是毫秒，而 types.ts 要的是百分数与秒，
 * 换算（×100 / ÷1000）收在映射函数里，页面拿到的一律是 types.ts 的语义。
 *
 * **Token 监控（K1–K4）同样已接真实接口**。KPI 环比按契约拍板不做；`provider` 后端没有
 * 该字段，模型明细不再展示。
 */
import apiClient from './request';
import {
  PM_ALL,
  PM_OBJECT_FULL_MAX_PAGES,
  PM_OBJECT_FULL_PAGE_SIZE,
  PM_OBJECT_PAGE_SIZE,
  PM_PAGE_SIZE,
} from '../constants/platformMonitor';
import { PM_TIME_PATTERN, fmtPmTime } from '../pages/PlatformMonitor/time';
import type {
  PmCallAttempt,
  PmCallDetail,
  PmCallDirection,
  PmCallMetricMode,
  PmCallObjectDetail,
  PmCallProtocol,
  PmCallRecord,
  PmCallRecordFilters,
  PmCallScopeRow,
  PmCallStats,
  PmCallStatus,
  PmCallTrigger,
  PmFailureReason,
  PmMetaFilters,
  PmModelShareItem,
  PmModelUsageRow,
  PmStageKey,
  PmTaskFailure,
  PmTaskOverviewRow,
  PmTaskStats,
  PmTaskType,
  PmTokenStats,
  PmTrendMetric,
  PmTrendPoint,
  PmWindow,
  PmWindowPreset,
} from '../pages/PlatformMonitor/types';

/* ══════════════════════ 任务监控（真实接口 T1–T4） ══════════════════════ */

/**
 * 任务侧 interface DTO。字段名照接口原文，量纲也照接口原文（比率 0–1、时长毫秒），
 * 换算只在下面的映射函数里做一次。
 */
interface TaskSummaryDto {
  total_tasks: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  /** 实时快照：当前等待（pending）任务数，不套时间窗 */
  waiting_now: number;
  incomplete_count: number;
  as_of: string;
  /** 与 C1/C3/K1 同形：`module_error` 非空 = 后端降级，不是「确实没数据」 */
  flags?: MonitorFlagsDto | null;
}

interface FailedTaskDto {
  task_id: string;
  task_name: string | null;
  /** 任务目录 code（`task_center.task_catalog_code`，如 `file_processing_ontology`），可增，非固定枚举 */
  task_type: string;
  status: string;
  error_message: string | null;
  finished_at: string | null;
  /** 后端已把 15 个 error_code 归并成前端 5 档，直接取用，前端不再自算 */
  reason: string;
}

interface TaskStageDto {
  stage_code: string;
  stage_name: string;
  exec_count: number;
  running_count: number;
  waiting_count: number;
  failed_count: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
}

interface TaskTypeOverviewDto {
  task_type: string;
  total_count: number;
  running_count: number;
  waiting_count: number;
  failed_count: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  stages: TaskStageDto[];
}

/** 0–1 小数比率 → 百分数；null（空窗口）原样透传，⛔ 不兜 0 */
const toPercent = (rate: number | null): number | null => (rate == null ? null : rate * 100);

/** 毫秒 → 秒（取整）；null 原样透传 */
const toSeconds = (ms: number | null): number | null => (ms == null ? null : Math.round(ms / 1000));

/** 任务侧四档指标 → 接口 `metric`（与调用侧的取值**不同名**，故单列一张表） */
const TASK_TREND_METRIC: Record<PmTrendMetric, string> = {
  volume: 'task_count',
  successRate: 'success_rate',
  avgDuration: 'avg_duration',
  failures: 'failed_count',
};

/** T1 任务统计卡 */
export const getTaskStats = async (window: PmWindow): Promise<PmTaskStats> => {
  const dto = await apiClient.get<TaskSummaryDto>('/platform/monitor/tasks/summary', {
    params: pmRange(window),
  });
  return {
    total: dto.total_tasks,
    successRate: toPercent(dto.success_rate),
    avgDurationSec: toSeconds(dto.avg_duration_ms),
    waiting: dto.waiting_now,
    moduleError: dto.flags?.module_error ?? null,
    asOf: dto.as_of ?? '',
  };
};

/**
 * T2 任务趋势。
 *
 * 与调用侧 `getCallTrend` 同构：`metric` 改名、`bucket` 按窗口换算，**成功率点两侧都 ×100**
 * （接口给 0–1 小数，否则图上刻度会是 0.96 而不是 96）。差别只在**耗时量纲** —— 任务耗时按秒
 * 展示（÷1000），调用耗时保留毫秒（调用延迟普遍在百毫秒级，换秒会全变 0，且卡片 / 表格的
 * `fmtLatency` 也按 ms）。空桶 `value=null` 原样透传，由图表断成缺口（⛔ 兜 0 会画出贴底假线）。
 */
export const getTaskTrend = async (
  window: PmWindow,
  metric: PmTrendMetric,
  /** 任务目录 code（M1 `task_types[].code`）；不传 = 全部任务 */
  taskType?: string,
): Promise<PmTrendPoint[]> => {
  const bucket = pmBucket(window);
  const data = await apiClient.get<{ points: TrendPointDto[] }>('/platform/monitor/tasks/trend', {
    params: {
      ...pmRange(window),
      metric: TASK_TREND_METRIC[metric],
      bucket,
      ...(taskType ? { task_type: taskType } : {}),
    },
  });
  return (data.points ?? []).map((point) => ({
    // 横轴按平台时区出刻度：任务族接口给的是 +08:00，浏览器不在东八区时不能直接 format
    date:
      fmtPmTime(point.bucket_start, bucket === 'hour' ? PM_TIME_PATTERN.bucketHour : PM_TIME_PATTERN.bucketDay) ??
      point.bucket_start,
    value:
      metric === 'successRate'
        ? toPercent(point.value)
        : metric === 'avgDuration'
          ? toSeconds(point.value)
          : point.value,
    closed: point.closed,
  }));
};

export interface PmTaskFailurePage {
  items: PmTaskFailure[];
  total: number;
}

/**
 * T3 失败任务列表。
 *
 * 本页没有分页控件，恒取第一页 5 条（契约 `page_size` 默认即 5）；标题上的失败总数徽标
 * 读接口回的 `total` —— **不能再拿 T4 概览的 `failed` 列求和**：那是未筛选的全局数，
 * 按任务类型筛过的列表若配一个全局总量，两个数必然对不上。
 */
export const getTaskFailures = async (
  window: PmWindow,
  /** 任务目录 code（M1 `task_types[].code`）；不传 = 全部任务 */
  taskType?: string,
): Promise<PmTaskFailurePage> => {
  const data = await apiClient.get<{ items: FailedTaskDto[]; total: number }>(
    '/platform/monitor/tasks/failures',
    {
      params: {
        ...pmRange(window),
        ...(taskType ? { task_type: taskType } : {}),
        page: 1,
        page_size: PM_PAGE_SIZE,
      },
    },
  );
  return {
    total: data.total ?? 0,
    items: (data.items ?? []).map((dto) => ({
      taskId: dto.task_id,
      // 缺失任务名时落占位符：空串在表格里读起来像「加载失败」
      name: dto.task_name ?? '-',
      taskType: dto.task_type,
      reason: dto.reason as PmFailureReason,
      // 时间窗可能是 7 天，只给 HH:mm 会看不出是哪天，所以带上月日；时区统一走平台时区
      time: fmtPmTime(dto.finished_at, PM_TIME_PATTERN.minute) ?? '-',
    })),
  };
};

/**
 * T4 任务运行概览（父表按任务类型分组 + 嵌套 stages[]）。
 *
 * `task_type` 是任务目录 code（如 `file_processing_ontology`），**不是固定枚举**；
 * 展示名取 M1 `task_types[].name`。stage 同理带出 `stage_name`，不再依赖前端 i18n。
 */
export const getTaskOverview = async (window: PmWindow): Promise<PmTaskOverviewRow[]> => {
  const data = await apiClient.get<{ items: TaskTypeOverviewDto[] }>(
    '/platform/monitor/tasks/overview',
    { params: pmRange(window) },
  );
  return (data.items ?? []).map((dto) => ({
    taskType: dto.task_type as PmTaskType,
    total: dto.total_count,
    running: dto.running_count,
    waiting: dto.waiting_count,
    failed: dto.failed_count,
    successRate: toPercent(dto.success_rate),
    avgDurationSec: toSeconds(dto.avg_duration_ms),
    stages: (dto.stages ?? []).map((stage) => ({
      stage: stage.stage_code as PmStageKey,
      stageName: stage.stage_name,
      running: stage.running_count,
      waiting: stage.waiting_count,
      failed: stage.failed_count,
      successRate: toPercent(stage.success_rate),
      avgDurationSec: toSeconds(stage.avg_duration_ms),
    })),
  }));
};

/* ══════════════════════ 接口 DTO（私有，页面不该看见） ══════════════════════ */

/** 公共标志位（C1/C3/T1/K1 同形） */
interface MonitorFlagsDto {
  data_delayed?: boolean;
  demo_data?: boolean;
  module_error?: string | null;
}

/** 统计口径元数据：`metric_mode='degraded'` 时数字按物理 attempt 计 */
interface CallModeMetaDto {
  metric_mode?: string | null;
}

interface CallSummaryDto {
  total_calls: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  failed_count: number;
  mode_meta?: CallModeMetaDto | null;
  flags?: MonitorFlagsDto | null;
  as_of?: string;
}

/** 对象 / 能力列表分页出参：在 `items` + `total` 之外附带同一份口径元数据 */
interface CallObjectPageDto {
  items: CallObjectDto[];
  total: number;
  mode_meta?: CallModeMetaDto | null;
}

/** 调用趋势出参：趋势点 + 本窗口的统计口径 */
interface CallTrendDto {
  points: TrendPointDto[];
  mode_meta?: CallModeMetaDto | null;
}

/** 调用记录分页出参：`mode_meta` 由后端按整次查询统一判定（降级 = 行来自物理 attempt） */
interface CallRecordPageDto {
  items: CallRecordDto[];
  total: number;
  mode_meta?: CallModeMetaDto | null;
}

interface CallObjectDto {
  /** 对象标识：模型名 / 能力 ID（物理列 `monitor.call_log.target`），表格与筛选都用它 */
  object_id: string | null;
  /** 对象类型：由 protocol 推导，只有 `openai` / `invoke` 两类 —— **不是对象名** */
  object_type: string;
  protocol: string | null;
  call_count: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  failed_count: number;
  last_called_at: string | null;
}

interface CallRecordDto {
  request_id: string;
  /**
   * 本行对应 attempt 的 id。降级模式下同一个 `request_id` 会出现多行，只有它能区分；
   * 正常模式后端给 `null`（那时 `request_id` 自身即唯一）。
   */
  attempt_id: string | null;
  /** 链路关联 ID；老数据 / 采集链路未打通时为 null */
  trace_id?: string | null;
  direction: string;
  trigger_type: string;
  caller: string | null;
  target: string;
  protocol: string | null;
  status: string;
  error_code: string | null;
  duration_ms: number | null;
  logical_started_at: string;
}

interface TrendPointDto {
  bucket_start: string;
  /**
   * 指标值。**比率 / 均值类空桶返 `null`** —— `success_rate` / `avg_latency` 在一格内没有任何调用时
   * 是「无数据」，与「0%」语义相反，后端 `dtos/monitor.py` 明令禁止兜 0。计数类（`call_count` /
   * `failed_count`）恒有值。null 一路透传进趋势图，由 G2 断成缺口（实测：断口 ≠ 贴底平线）。
   */
  value: number | null;
  closed: boolean;
}

interface CallDetailDto {
  request_id: string;
  method: string | null;
  path: string | null;
  status_code: number | null;
  error_message: string | null;
  request_payload: string | null;
  response_payload: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  stage: number | null;
  /** 采集元数据 / 步骤（原样回传，脱敏后）。本期恒为 null */
  meta?: Record<string, unknown> | null;
  /** 本条记录取自逻辑调用还是物理 attempt（降级时 `degraded`） */
  mode_meta?: CallModeMetaDto | null;
}

interface AttemptDto {
  attempt_id: string;
  attempt_no: number;
  attempt_started_at: string | null;
  attempt_ended_at: string | null;
  duration_ms: number | null;
  status: string;
  error_code: string | null;
  error_message: string | null;
  is_final: boolean;
  wait_before_ms: number | null;
}

interface ObjectDetailDto {
  object_id: string;
  object_type: string;
  call_count: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  failed_count: number;
  top_sources: { name: string; call_count: number }[];
  recent_failures: { request_id: string; error_message: string | null; logical_started_at: string | null }[];
  /** 基础指标按逻辑调用还是物理 attempt 统计（降级时 `degraded`） */
  mode_meta?: CallModeMetaDto | null;
}

interface MetaFiltersDto {
  task_types?: { code: string; name: string }[];
  /** 「超时」这一档没有可下发的筛选码，`code` 为 null */
  statuses?: { code: string | null; label: string; pm_label: string; in_denominator: boolean }[];
  objects?: { object_type: string; protocol: string | null }[];
  call_sources?: string[];
}

/* ══════════════════════ 映射：接口字段 → types.ts ══════════════════════ */

/**
 * 只认契约里的两个取值：别的字符串（含缺失）一律归 null，
 * 页面就不会因为后端新增枚举而误报口径。
 */
const toMetricMode = (value?: string | null): PmCallMetricMode | null =>
  value === 'logical' || value === 'degraded' ? value : null;

const mapSummary = (dto: CallSummaryDto): PmCallStats => ({
  total: dto.total_calls,
  successRate: toPercent(dto.success_rate),
  avgLatencyMs: dto.avg_latency_ms,
  failed: dto.failed_count,
  moduleError: dto.flags?.module_error ?? null,
  metricMode: toMetricMode(dto.mode_meta?.metric_mode),
  asOf: dto.as_of ?? '',
});

const mapObject = (dto: CallObjectDto): PmCallScopeRow => ({
  // `object_type` 是 openai/invoke 这类分类码，不能拿来当名字；兜底只在 object_id 缺失时发生
  name: dto.object_id ?? dto.object_type,
  calls: dto.call_count,
  successRate: toPercent(dto.success_rate),
  avgLatencyMs: dto.avg_latency_ms,
  failed: dto.failed_count,
  lastCallAt: fmtPmTime(dto.last_called_at, PM_TIME_PATTERN.second),
  protocol: (dto.protocol as PmCallProtocol | null) ?? null,
});

const mapRecord = (dto: CallRecordDto): PmCallRecord => ({
  time: fmtPmTime(dto.logical_started_at, PM_TIME_PATTERN.second) ?? '-',
  direction: dto.direction as PmCallDirection,
  attemptId: dto.attempt_id,
  triggerType: dto.trigger_type as PmCallTrigger,
  caller: dto.caller,
  target: dto.target,
  // 前端的「调用方式」列装的就是 protocol，不是 HTTP method（见 types.ts 该字段说明）
  method: (dto.protocol as PmCallProtocol | null) ?? null,
  status: dto.status as PmCallStatus,
  latencyMs: dto.duration_ms,
  errorCode: dto.error_code,
  requestId: dto.request_id,
  traceId: dto.trace_id ?? null,
});

const mapAttempt = (dto: AttemptDto): PmCallAttempt => ({
  attemptId: dto.attempt_id,
  attemptNo: dto.attempt_no,
  // 重试的各 attempt 之间可能只差几百毫秒，所以这里比列表多带一段毫秒
  attemptStartedAt: fmtPmTime(dto.attempt_started_at, PM_TIME_PATTERN.millisecond),
  attemptEndedAt: fmtPmTime(dto.attempt_ended_at, PM_TIME_PATTERN.millisecond),
  durationMs: dto.duration_ms,
  status: dto.status as PmCallStatus,
  errorCode: dto.error_code,
  errorMessage: dto.error_message,
  isFinal: dto.is_final,
  waitBeforeMs: dto.wait_before_ms,
});

/* ══════════════════════ 请求参数换算 ══════════════════════ */

/** 调用监控的两个统计口径：外部依赖（调用对象）/ 开放能力 */
export type PmCallScope = 'target' | 'capability';

/**
 * 时间窗 → 接口的 `from` / `to` / `range_preset`。
 *
 * 契约要求 `from`/`to` 必填且是「唯一时间真值」，`range_preset` 只是「用户选了哪一档」的标签、
 * 不参与计算。页面仍只传 `window`，换算收在这里，所以换时间控件时页面不用动。
 */
const WINDOW_HOURS: Record<PmWindowPreset, number> = {
  '1h': 1,
  '24h': 24,
  '7d': 7 * 24,
  '30d': 30 * 24,
};

function pmRange(window: PmWindow, now = new Date()) {
  if (typeof window === 'object') {
    return { from: window.from, to: window.to, range_preset: window.preset };
  }
  return {
    from: new Date(now.getTime() - WINDOW_HOURS[window] * 3600_000).toISOString(),
    to: now.toISOString(),
    range_preset: window,
  };
}

/**
 * 时间窗 → 绝对区间（ISO）。
 *
 * 给「把当前窗口带给别的页面」用（监控页 → 任务中心的失败任务深链）：对方只认 from/to，
 * 不认识 1h/24h 这类预设标签。
 */
export const pmWindowBounds = (window: PmWindow): { from: string; to: string } => {
  const { from, to } = pmRange(window);
  return { from, to };
};

/**
 * 桶粒度与横轴刻度一致：两档日内的窗口按小时，更长的按天。
 * 自定义区间按实际跨度判断（48h 为界），否则 30 天自定义会被切成 720 个小时桶。
 */
const pmBucket = (window: PmWindow): 'hour' | 'day' => {
  if (typeof window === 'object') {
    const spanHours = (Date.parse(window.to) - Date.parse(window.from)) / 3600_000;
    return spanHours <= 48 ? 'hour' : 'day';
  }
  return window === '1h' || window === '24h' ? 'hour' : 'day';
};

/** 页面侧的四档指标 → 接口的 metric 取值（两边命名不同，映射收在这里） */
const TREND_METRIC: Record<PmTrendMetric, string> = {
  volume: 'call_count',
  successRate: 'success_rate',
  avgDuration: 'avg_latency',
  failures: 'failed_count',
};

/* ══════════════════════ 调用监控（真实接口） ══════════════════════ */

export interface PmCallScopePage {
  items: PmCallScopeRow[];
  total: number;
  /** 本页数字的统计口径（与同页统计卡同源）；null = 后端未声明 */
  metricMode: PmCallMetricMode | null;
}

/**
 * C1 外部依赖统计卡。
 *
 * ⚠️ 它统计的是 `direction=outbound`（平台调出去）。若台账里还没有出站调用，
 * 这里返回全 0 是**正常**的，不等于接口坏了。
 */
export const getCallStats = async (window: PmWindow): Promise<PmCallStats> =>
  mapSummary(
    await apiClient.get<CallSummaryDto>('/platform/monitor/calls/external/summary', {
      params: pmRange(window),
    }),
  );

/** C3 开放能力统计卡（`direction=inbound`，即外部调平台） */
export const getCapabilityStats = async (window: PmWindow): Promise<PmCallStats> =>
  mapSummary(
    await apiClient.get<CallSummaryDto>('/platform/monitor/calls/open/summary', {
      params: pmRange(window),
    }),
  );

/** C2 外部依赖对象列表 */
export const getCallTargets = async (
  window: PmWindow,
  page = 1,
  pageSize = PM_OBJECT_PAGE_SIZE,
): Promise<PmCallScopePage> => {
  const data = await apiClient.get<CallObjectPageDto>('/platform/monitor/calls/external/objects', {
    params: { ...pmRange(window), page, page_size: pageSize },
  });
  return {
    items: (data.items ?? []).map(mapObject),
    total: data.total ?? 0,
    metricMode: toMetricMode(data.mode_meta?.metric_mode),
  };
};

/** C4 开放能力列表（比 C2 多一档 `protocol` 筛选） */
export const getOpenCapabilities = async (
  window: PmWindow,
  page = 1,
  pageSize = PM_OBJECT_PAGE_SIZE,
  protocol?: PmCallProtocol,
): Promise<PmCallScopePage> => {
  const data = await apiClient.get<CallObjectPageDto>('/platform/monitor/calls/open/objects', {
    params: {
      ...pmRange(window),
      page,
      page_size: pageSize,
      ...(protocol ? { protocol } : {}),
    },
  });
  return {
    items: (data.items ?? []).map(mapObject),
    total: data.total ?? 0,
    metricMode: toMetricMode(data.mode_meta?.metric_mode),
  };
};

/**
 * 拉取整个时间窗内的**全量**对象 / 能力清单（外部依赖 / 开放能力对象列表接口），供趋势卡下拉使用。
 *
 * 明细表一页只有 20 条，拿当前页当选项时对象一多或翻页后趋势卡就选不全。
 * 这里按接口上限（`page_size=100`）逐页拉全，与明细表分页彻底解耦；
 * 对象标识是窗口内出现过的 target，本身有界，不需要新增无界的「全量」接口。
 *
 * `metricMode` 取第一页：同一窗口的整次查询口径统一。触达页数护栏时保留已取到的
 * 部分并在控制台告警，不静默把清单截断成「看起来就这么多」。
 */
export const getAllCallScopeObjects = async (
  window: PmWindow,
  scope: PmCallScope,
): Promise<PmCallScopePage> => {
  const items: PmCallScopeRow[] = [];
  let total = 0;
  let metricMode: PmCallMetricMode | null = null;

  for (let page = 1; ; page += 1) {
    const data =
      scope === 'target'
        ? await getCallTargets(window, page, PM_OBJECT_FULL_PAGE_SIZE)
        : await getOpenCapabilities(window, page, PM_OBJECT_FULL_PAGE_SIZE);
    if (page === 1) metricMode = data.metricMode;
    total = data.total;
    items.push(...data.items);

    // 已拉满 / 窗口内对象变少导致空页（正常不会发生，防死循环）
    if (items.length >= total || data.items.length === 0) break;
    if (page >= PM_OBJECT_FULL_MAX_PAGES) {
      console.warn(
        `[platform-monitor] 全量对象清单超过 ${PM_OBJECT_FULL_MAX_PAGES} 页护栏，仅展示前 ${items.length} 个（共 ${total}）`,
      );
      break;
    }
  }

  return { items, total, metricMode };
};

/**
 * C5 调用趋势。
 *
 * ⚠️ **两个页签必须传 `direction`**（外部依赖 `outbound` / 开放能力 `inbound`）：C5 是这两个
 * 二级页签**共用的一条接口**，不传拿到的是全量曲线，两页签会画出同一条线 —— 一条线劈成两条
 * 等于编造后端没给的数据，前端不做这种补偿。
 *
 * `objectId` 传具体对象则只画那一条，不传即全量；按单个对象看时 `direction` 可省。
 *
 * 返回值带 `metricMode`：趋势与同页统计卡同源，页面据此给出统一的口径提示。
 */
export interface PmCallTrend {
  points: PmTrendPoint[];
  metricMode: PmCallMetricMode | null;
}

export const getCallTrend = async (
  window: PmWindow,
  metric: PmTrendMetric,
  direction?: PmCallDirection,
  objectId?: string,
): Promise<PmCallTrend> => {
  const bucket = pmBucket(window);
  const data = await apiClient.get<CallTrendDto>('/platform/monitor/calls/trend', {
    params: {
      ...pmRange(window),
      metric: TREND_METRIC[metric],
      bucket,
      ...(direction ? { direction } : {}),
      ...(objectId && objectId !== PM_ALL ? { object_id: objectId } : {}),
    },
  });
  return { points: mapTrendPoints(data.points, metric, bucket), metricMode: toMetricMode(data.mode_meta?.metric_mode) };
};

/** 趋势点映射：刻度按平台时区、成功率 ×100，空桶 null 原样保留 */
const mapTrendPoints = (
  points: TrendPointDto[] | undefined,
  metric: PmTrendMetric,
  bucket: 'hour' | 'day',
): PmTrendPoint[] =>
  (points ?? []).map((point) => ({
    // 横轴刻度由接口的桶起点决定，粒度与 bucket 一致；时区统一走平台时区（接口给 UTC `Z`）
    date:
      fmtPmTime(point.bucket_start, bucket === 'hour' ? PM_TIME_PATTERN.bucketHour : PM_TIME_PATTERN.bucketDay) ??
      point.bucket_start,
    // 成功率点 ×100（接口给 0–1 小数），否则图上刻度是 0.96 而不是 96 —— 与 `getTaskTrend` 一致。
    // 耗时点**不换算**：调用延迟本就是毫秒，且卡片 / 表格的 `fmtLatency` 也按 ms，换秒会全变 0。
    value: metric === 'successRate' ? toPercent(point.value) : point.value,
    closed: point.closed,
  }));

/** 筛选值见 `PmCallRecordFilters`（与列表页的 CommonTable 共用同一份定义），这里只补分页。 */
export interface PmCallRecordQuery extends PmCallRecordFilters {
  page: number;
  pageSize: number;
}

export interface PmCallRecordPage {
  items: PmCallRecord[];
  total: number;
  /** 整次查询统一的口径；旧后端不返回时由 `attemptId` 推断（见页面） */
  metricMode: PmCallMetricMode | null;
}

/**
 * C6 调用记录列表。
 *
 * 筛选与分页都下发到后端（真实记录表是分页查询，拉全量再在浏览器里筛不成立），
 * 空值一律不传该 key —— 这是本应用既有的参数拼装惯例（见 `src/api/tenants.ts`）。
 * 排序交给后端默认的 `logical_started_at desc`，前端不传 `sort`。
 */
export const getCallRecords = async (window: PmWindow, query: PmCallRecordQuery): Promise<PmCallRecordPage> => {
  const data = await apiClient.get<CallRecordPageDto>(
    '/platform/monitor/calls/records',
    {
      params: {
        ...pmRange(window),
        ...(query.direction !== 'all' ? { direction: query.direction } : {}),
        ...(query.status !== 'all' ? { status: query.status } : {}),
        ...(query.triggerType !== 'all' ? { trigger_type: query.triggerType } : {}),
        ...(query.protocol !== 'all' ? { protocol: query.protocol } : {}),
        ...(query.source ? { source: query.source } : {}),
        // 精确匹配（对象详情下钻），与 keyword 的模糊匹配是两套语义
        ...(query.target ? { target: query.target } : {}),
        ...(query.keyword ? { keyword: query.keyword } : {}),
        page: query.page,
        page_size: query.pageSize,
      },
    },
  );
  return {
    items: (data.items ?? []).map(mapRecord),
    total: data.total ?? 0,
    metricMode: toMetricMode(data.mode_meta?.metric_mode),
  };
};

/**
 * C7 单次调用详情。
 *
 * `attemptId` 只在**降级模式**下必要（那时 `request_id` 会重复，必须靠 attempt 定位），
 * 由调用记录行回显（`PmCallRecord.attemptId`，正常模式为 null）。降级模式下缺失该参数
 * 会返回 400 + 1001，不会「取同 request_id 最早一条」。
 */
export const getCallDetail = async (requestId: string, attemptId?: string): Promise<PmCallDetail> => {
  const dto = await apiClient.get<CallDetailDto>(
    `/platform/monitor/calls/${encodeURIComponent(requestId)}/detail`,
    { params: attemptId ? { attempt_id: attemptId } : {} },
  );
  return {
    requestId: dto.request_id,
    method: dto.method,
    path: dto.path,
    statusCode: dto.status_code,
    errorMessage: dto.error_message,
    requestPayload: dto.request_payload,
    responsePayload: dto.response_payload,
    promptTokens: dto.prompt_tokens,
    completionTokens: dto.completion_tokens,
    totalTokens: dto.total_tokens,
    stage: dto.stage,
    meta: dto.meta ?? null,
    metricMode: toMetricMode(dto.mode_meta?.metric_mode),
  };
};

export interface PmCallAttemptPage {
  items: PmCallAttempt[];
  total: number;
  /** `max(attempt_count - 1, 0)`，由后端算好 */
  retryCount: number;
}

/** C8 某次逻辑调用下的 attempt 明细 */
export const getCallAttempts = async (
  requestId: string,
  page = 1,
  pageSize = 20,
): Promise<PmCallAttemptPage> => {
  const data = await apiClient.get<{ items: AttemptDto[]; total: number; retry_count: number }>(
    `/platform/monitor/calls/${encodeURIComponent(requestId)}/attempts`,
    { params: { page, page_size: pageSize } },
  );
  return {
    items: (data.items ?? []).map(mapAttempt),
    total: data.total ?? 0,
    retryCount: data.retry_count ?? 0,
  };
};

/**
 * C9 对象 / 能力详情。
 *
 * ⚠️ 接口对「对象不存在」与「对象存在但所选时间窗内没有任何调用」**返回同一个 404 + 1002**，
 * 调用方按「无此对象」给 empty 态即可，不要渲染成全 0 的空壳。
 */
export const getCallObjectDetail = async (
  objectId: string,
  window: PmWindow,
  protocol?: PmCallProtocol,
): Promise<PmCallObjectDetail> => {
  const dto = await apiClient.get<ObjectDetailDto>(
    `/platform/monitor/calls/objects/${encodeURIComponent(objectId)}/detail`,
    { params: { ...pmRange(window), ...(protocol ? { protocol } : {}) } },
  );
  return {
    objectId: dto.object_id,
    objectType: dto.object_type,
    callCount: dto.call_count,
    successRate: toPercent(dto.success_rate),
    avgLatencyMs: dto.avg_latency_ms,
    failedCount: dto.failed_count,
    topSources: (dto.top_sources ?? []).map((item) => ({ name: item.name, callCount: item.call_count })),
    recentFailures: (dto.recent_failures ?? []).map((item) => ({
      requestId: item.request_id,
      errorMessage: item.error_message,
      time: fmtPmTime(item.logical_started_at, PM_TIME_PATTERN.second),
    })),
    metricMode: toMetricMode(dto.mode_meta?.metric_mode),
  };
};

/**
 * M1 筛选元数据（下拉候选）。
 *
 * **失败一律返回 null，不抛异常**：下拉候选属锦上添花，不能让它把整页拖进错误态，
 * 所以这里吞掉异常、由各页面回退到静态候选。
 */
export const getMetaFilters = async (): Promise<PmMetaFilters | null> => {
  try {
    const dto = await apiClient.get<MetaFiltersDto>('/platform/monitor/meta/filters');
    return {
      taskTypes: dto.task_types ?? [],
      statuses: (dto.statuses ?? []).map((item) => ({
        code: item.code,
        label: item.label,
        pmLabel: item.pm_label,
        inDenominator: item.in_denominator,
      })),
      objects: (dto.objects ?? []).map((item) => ({ objectType: item.object_type, protocol: item.protocol })),
      callSources: dto.call_sources ?? [],
    };
  } catch {
    return null;
  }
};

/* ══════════════════════ Token 监控（真实接口 K1–K4） ══════════════════════ */

/**
 * Token 侧 DTO。字段名照接口原文；与任务/调用侧不同，这里的量纲无需换算
 * （接口直接给 token 数与次数），只有时间串要在映射层格式化。
 */
interface TokenSummaryDto {
  call_count: number;
  total_tokens: number;
  avg_tokens_per_call: number | null;
  model_count: number;
  completeness_rate: number | null;
  as_of: string;
  /** 与 C1/C3/T1 同形：`module_error` 非空 = 后端降级，不是「确实没数据」 */
  flags?: MonitorFlagsDto | null;
}

interface TokenTrendPointDto {
  bucket_start: string;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

interface TokenModelTopDto {
  model_id: string;
  total_tokens: number;
  ratio: number | null;
}

interface TokenUsageDto {
  model_id: string;
  model_name: string;
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  avg_tokens_per_call: number | null;
  last_called_at: string | null;
  /** 未回传用量的调用数；老后端不带该字段时按 0 处理 */
  unmetered_count?: number;
}

/** Token 趋势的三种口径 */
export type PmTokenTrendKind = 'total' | 'input' | 'output';

/** 趋势三口径 → 接口字段（K2 恒返四个量，选哪个由前端取哪一列决定） */
const TOKEN_TREND_FIELD: Record<PmTokenTrendKind, 'total_tokens' | 'prompt_tokens' | 'completion_tokens'> = {
  total: 'total_tokens',
  input: 'prompt_tokens',
  output: 'completion_tokens',
};

/**
 * K1 Token 用量统计卡。
 *
 * 「较上一周期 +x%」按契约拍板**不做**（上一周期口径未定），所以这里只取当前窗口一次，
 * 不再并发上一窗口去凑环比。
 */
export const getTokenStats = async (window: PmWindow): Promise<PmTokenStats> => {
  const dto = await apiClient.get<TokenSummaryDto>('/platform/monitor/tokens/summary', {
    params: pmRange(window),
  });
  return {
    modelCalls: dto.call_count,
    totalTokens: dto.total_tokens,
    avgTokensPerCall: dto.avg_tokens_per_call,
    modelCount: dto.model_count,
    // 与前两族同一条规矩：0–1 小数 → 百分数；分母为 0 时后端给 null，不得兜 0
    completenessRate: toPercent(dto.completeness_rate),
    moduleError: dto.flags?.module_error ?? null,
    asOf: dto.as_of ?? '',
  };
};

/** K2 Token 用量趋势（`metric` 只做校验，响应恒返四个量） */
export const getTokenTrend = async (window: PmWindow, kind: PmTokenTrendKind): Promise<PmTrendPoint[]> => {
  const bucket = pmBucket(window);
  const field = TOKEN_TREND_FIELD[kind];
  const data = await apiClient.get<{ points: TokenTrendPointDto[] }>('/platform/monitor/tokens/trend', {
    params: { ...pmRange(window), metric: kind, bucket },
  });
  return (data.points ?? []).map((point) => ({
    // 刻度由接口的桶起点决定，粒度与 bucket 一致（与 C5/T2 同一处理）
    date:
      fmtPmTime(point.bucket_start, bucket === 'hour' ? PM_TIME_PATTERN.bucketHour : PM_TIME_PATTERN.bucketDay) ??
      point.bucket_start,
    value: point[field],
    // 接口恒返四个量、只按 metric 校验不裁剪：另外三个塞进 tooltip，不必再发一次请求
    detail: {
      input: point.prompt_tokens,
      output: point.completion_tokens,
      calls: point.call_count,
    },
  }));
};

/**
 * K3 模型使用分布。
 *
 * 接口按 token 总量取 TOP N，零用量的模型也会占一个名次；分布图里它们只会画出一条 0% 的空条，
 * 故在映射层剔掉。
 */
export const getModelShare = async (window: PmWindow): Promise<PmModelShareItem[]> => {
  const data = await apiClient.get<{ items: TokenModelTopDto[] }>('/platform/monitor/tokens/models/top', {
    params: { ...pmRange(window), limit: 5 },
  });
  return (data.items ?? [])
    .filter((item) => item.total_tokens > 0)
    .map((item) => ({ key: item.model_id, label: item.model_id, count: item.total_tokens }));
};

export interface PmModelUsagePage {
  rows: PmModelUsageRow[];
  total: number;
}

/**
 * 服务端排序字段（K4 `sort_by` 白名单；除「未回传用量」外各列均可排）。
 *
 * 写成运行时数组而不是纯类型：URL 里的 `sort_by` 是用户可改的字符串，
 * 读回来时必须按白名单校验，否则会把脏值直接下发给后端。
 */
export const PM_MODEL_USAGE_SORT_FIELDS = [
  'total_tokens',
  'call_count',
  'prompt_tokens',
  'completion_tokens',
  'avg_tokens_per_call',
  'last_called_at',
  'model',
] as const;

export type PmModelUsageSortField = (typeof PM_MODEL_USAGE_SORT_FIELDS)[number];

export interface PmModelUsageSort {
  field: PmModelUsageSortField;
  order: 'asc' | 'desc';
}

/**
 * K4 模型用量明细。
 *
 * 搜索、分页与排序都下发到后端：模型数会长期超过一页，且排序必须跨页才有意义
 * （只排当前页会得到「第 1 页的升序比第 2 页小」的假象）。空关键字不传该 key。
 */
export const getModelUsage = async (
  window: PmWindow,
  keyword: string,
  page = 1,
  pageSize = PM_PAGE_SIZE,
  sort?: PmModelUsageSort,
): Promise<PmModelUsagePage> => {
  const kw = keyword.trim();
  const data = await apiClient.get<{ items: TokenUsageDto[]; total: number }>('/platform/monitor/tokens/usage', {
    params: {
      ...pmRange(window),
      ...(kw ? { keyword: kw } : {}),
      ...(sort ? { sort_by: sort.field, sort_order: sort.order } : {}),
      page,
      page_size: pageSize,
    },
  });
  return {
    rows: (data.items ?? []).map((dto) => ({
      name: dto.model_name,
      modelId: dto.model_id,
      calls: dto.call_count,
      inputTokens: dto.prompt_tokens,
      outputTokens: dto.completion_tokens,
      totalTokens: dto.total_tokens,
      avgTokensPerCall: dto.avg_tokens_per_call,
      lastCallAt: fmtPmTime(dto.last_called_at, PM_TIME_PATTERN.full) ?? '-',
      unmeteredCount: dto.unmetered_count ?? 0,
    })),
    total: data.total ?? 0,
  };
};
