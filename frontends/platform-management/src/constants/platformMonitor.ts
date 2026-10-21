import type {
  PmCallProtocol,
  PmCallRecordFilters,
  PmCallSubKey,
  PmTabKey,
  PmWindowPreset,
} from '../pages/PlatformMonitor/types';

/** 趋势图的线/填充色，三个页签统一 */
export const PM_CHART_COLOR = '#3b82f6';

/** 「全部 X」下拉的哨兵值 */
export const PM_ALL = 'all';

/** 明细表默认每页条数（模型用量表设计稿一页 5 行） */
export const PM_PAGE_SIZE = 5;

/**
 * 对象 / 能力表的每页条数。
 *
 * 比模型表大得多：设计稿里这两张表没有分页器、是要一屏看完的，一页 5 条会在只有几个对象的
 * 情况下也凭空长出一条分页栏。真接后端后按契约限制在 1..100，取 20 够用且不会天天触发翻页。
 */
export const PM_OBJECT_PAGE_SIZE = 20;

/**
 * 「全量对象清单」每页条数：接口 `page_size` 上限为 100，按上限取以减少往返。
 *
 * 趋势卡的对象下拉必须覆盖整个时间窗，不能只取明细表当前页（见 `CallScopeView`）；
 * 对象标识是窗口内出现过的 target，本身有界，按页拉全比新增一个无界响应接口更稳。
 */
export const PM_OBJECT_FULL_PAGE_SIZE = 100;

/**
 * 全量对象清单的页数护栏（20 页 × 100 条 = 2000 个对象）。
 *
 * 正常平台远达不到这个量级；护栏只防异常数据把前端拖进无限翻页，
 * 触顶时 `CallScopeView` 会保留已取到的部分并在控制台告警，不静默截断。
 */
export const PM_OBJECT_FULL_MAX_PAGES = 20;

/** 调用记录表的每页条数。记录是真会堆到几千条的表，一页 5 条翻不过来，取 20 */
export const PM_RECORD_PAGE_SIZE = 20;

/** 调用记录表 Request ID 列宽：`rt-terminal:<uuid>` 这类最长 48 字符，按它能放下给 */
export const PM_RECORD_REQUEST_ID_WIDTH = 320;

/** 调用记录表 Trace ID 列宽。长度与 request_id 同量级，但它是**可选**列，给得略窄 */
export const PM_RECORD_TRACE_ID_WIDTH = 240;

/**
 * 调用记录表的横向最小宽度，**必须等于各列 width 之和**（含留给 target 的 220）。
 *
 * 固定表头是「表头、表体两张独立表格 + 同步滚动」，两边的可滚距离都由自己的内容宽度算；
 * 这个值小于列宽之和时，两张表内容宽度不一致，横滚行程就对不上，列会错位。
 */
export const PM_RECORD_SCROLL_X = 1780;

/** 一级页签顺序即 tab 栏顺序 */
export const PM_TAB_KEYS: readonly PmTabKey[] = ['task', 'call', 'token'];

export const PM_CALL_SUB_KEYS: readonly PmCallSubKey[] = ['external', 'capability', 'records'];

/**
 * 每个页签各自的默认时间窗。
 *
 * 不做成全局一个值：设计稿里任务/调用是「24小时」而 Token 是「近7天」，
 * 共用一个 state 会让切到 Token 时窗口被前一个页签带着走。
 */
export const PM_DEFAULT_WINDOW: Record<PmTabKey, PmWindowChoice> = {
  task: '24h',
  call: '24h',
  token: '7d',
};

/** 下拉值：预设档位，或「自定义」（由 RangePicker 提供 from/to） */
export type PmWindowChoice = PmWindowPreset | 'custom';

/**
 * 每个页签各自的可选档位。
 *
 * 契约：任务 / 调用 / Token = 1h / 24h / 7d / 30d / 自定义。
 * 仍按页签分开维护，避免未来某个页签的口径变化牵动另外两个。
 */
export const PM_WINDOW_OPTIONS: Record<PmTabKey, { value: PmWindowChoice; labelKey: string }[]> = {
  task: [
    { value: '1h', labelKey: 'platformMonitor.window.h1' },
    { value: '24h', labelKey: 'platformMonitor.window.h24' },
    { value: '7d', labelKey: 'platformMonitor.window.d7' },
    { value: '30d', labelKey: 'platformMonitor.window.d30' },
    { value: 'custom', labelKey: 'platformMonitor.window.custom' },
  ],
  call: [
    { value: '1h', labelKey: 'platformMonitor.window.h1' },
    { value: '24h', labelKey: 'platformMonitor.window.h24' },
    { value: '7d', labelKey: 'platformMonitor.window.d7' },
    { value: '30d', labelKey: 'platformMonitor.window.d30' },
    { value: 'custom', labelKey: 'platformMonitor.window.custom' },
  ],
  token: [
    { value: '1h', labelKey: 'platformMonitor.window.h1' },
    { value: '24h', labelKey: 'platformMonitor.window.h24' },
    { value: '7d', labelKey: 'platformMonitor.window.d7' },
    { value: '30d', labelKey: 'platformMonitor.window.d30' },
    { value: 'custom', labelKey: 'platformMonitor.window.custom' },
  ],
};

/** 自定义区间上限（契约：超过 30 天返回 4004） */
export const PM_MAX_CUSTOM_DAYS = 30;

/**
 * 调用记录筛选在 URL 上的键名（字段名 → query 参数名）。
 *
 * 七个筛选都写进 URL：刷新与分享链接能还原同一个视图。读写两侧都走这张表，
 * 不会出现「写进去叫 trigger、读出来查 triggerType」这种不一致。
 */
export const PM_RECORD_FILTER_PARAMS: Record<keyof PmCallRecordFilters, string> = {
  direction: 'direction',
  status: 'status',
  triggerType: 'trigger',
  protocol: 'protocol',
  source: 'source',
  target: 'target',
  keyword: 'keyword',
};

/* ══════════════════════ URL query 取值 ══════════════════════ */

/** 非法或缺省的 query 一律回落第一项，避免手改 URL 后白屏 */
export function toPmTabKey(value: string | null): PmTabKey {
  return PM_TAB_KEYS.includes(value as PmTabKey) ? (value as PmTabKey) : PM_TAB_KEYS[0];
}

export function toPmCallSubKey(value: string | null): PmCallSubKey {
  return PM_CALL_SUB_KEYS.includes(value as PmCallSubKey) ? (value as PmCallSubKey) : PM_CALL_SUB_KEYS[0];
}

/**
 * URL 里的 `win` → 窗口档位；非法值（如手改出的 `99d`）返回 null 交给默认值。
 *
 * 必须按页签校验：各页签的候选档位由 `PM_WINDOW_OPTIONS` 各自维护，
 * 全局认了非法值会让下拉显示一个不在候选里的值。
 */
export function toPmWindowChoice(tab: PmTabKey, value: string | null): PmWindowChoice | null {
  if (!value) return null;
  return PM_WINDOW_OPTIONS[tab].some((option) => option.value === value) ? (value as PmWindowChoice) : null;
}

/**
 * 失败数 KPI 下钻到记录页的 query。
 *
 * 方向与趋势同一套口径（外部依赖 = outbound，开放能力 = inbound）：记录页没有
 * 「对象类型」这一维，方向是唯一能把两边数字对上的筛选。
 * 抽成纯函数是为了让「口径映射」能被单测钉住 —— 拼错一个值，下钻后就是空的。
 */
export function pmFailureDrillParams(scope: 'target' | 'capability', current: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(current);
  // 下钻代表 KPI 的整窗口口径：先清掉记录页在 URL 上残留的其它筛选，
  // 否则「失败 42」点进去可能只剩 3 条（还带着上次选的 protocol / source）
  Object.values(PM_RECORD_FILTER_PARAMS).forEach((param) => next.delete(param));
  next.set('sub', 'records');
  next.set(PM_RECORD_FILTER_PARAMS.status, 'failed');
  next.set(PM_RECORD_FILTER_PARAMS.direction, scope === 'target' ? 'outbound' : 'inbound');
  return next;
}

/**
 * 对象详情「查看全部调用」下钻到记录页的 query。
 *
 * 时间窗留在原 query 上（`win` / `from` / `to` 由页面级工具条维护），这里只切页签并
 * 预填对象标识。`target` 是**精确匹配**，与 keyword 的模糊匹配分开 ——
 * 同前缀的两个对象（如 `svc-a` / `svc-ab`）不能互相串进来。
 *
 * 开放能力侧同一个对象名可能挂在多个协议下，点进来的那一行带 `protocol` 时一并预填，
 * 让记录页与对象详情是同一口径；外部依赖侧没有协议概念（`null`），不传。
 * 失败下钻留下的 `status` 不属于这次跳转，先清掉。
 */
export function pmObjectCallsDrillParams(
  objectId: string,
  protocol: PmCallProtocol | null,
  current: URLSearchParams,
): URLSearchParams {
  const next = new URLSearchParams(current);
  next.set('sub', 'records');
  next.set('target', objectId);
  next.delete('status');
  next.delete('direction');
  next.delete('protocol');
  if (protocol) next.set('protocol', protocol);
  return next;
}

/* 趋势横轴已改由接口的桶起点决定（见 `api/platformMonitor.ts`），这里不再按窗口摊刻度 */
