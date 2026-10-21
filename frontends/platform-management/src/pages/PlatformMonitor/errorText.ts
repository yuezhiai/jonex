/**
 * 监控读接口的错误码分流。
 *
 * 三类失败对用户的含义完全不同，只甩一句 `err.message` 会让人不知道下一步该做什么：
 *
 * | 码 | HTTP | 含义 | 用户该做什么 |
 * | --- | --- | --- | --- |
 * | 4004 | 409 | 区间跨度超上限 / 起点早于保留期 | 缩短范围或调整起点 |
 * | 3006 | 403 | 缺 `platform:monitor:read` | 找管理员要权限 |
 * | 4100 | 503 | 数据源缺失 / 历史重算未完成 | 稍后重试（重试也未必马上好） |
 * | 5001 | 503 | 依赖服务不可用 | 稍后重试 |
 *
 * 4004 有两种 `error_details.reason`：`range_too_wide`（跨度超限）与
 * `out_of_retention`（起点早于保留期）。后者带 `queryable_range`，要告诉用户实际可查区间，
 * 不能一律说「最多 N 天」。
 * 判定顺序按「越具体越前」：4100 带 503，必须排在「HTTP 503 ⇒ 上游不可用」之前；
 * 3006 与 403 同义，任一命中即权限问题。
 */

export type PmErrorKind =
  | 'rangeTooLong'
  | 'outOfRetention'
  | 'forbidden'
  | 'unavailable'
  | 'upstream'
  | 'unknown';

interface PmErrorLike {
  status?: number;
  bizCode?: number;
  errorDetails?: Record<string, unknown>;
}

/** 从任意异常里取出 HTTP 状态与业务码。非对象 / 非数字一律当作没有 */
function readErrorCode(error: unknown): PmErrorLike {
  if (!error || typeof error !== 'object') return {};
  const { status, bizCode, errorDetails } = error as PmErrorLike;
  return {
    status: typeof status === 'number' ? status : undefined,
    bizCode: typeof bizCode === 'number' ? bizCode : undefined,
    errorDetails: errorDetails && typeof errorDetails === 'object' ? errorDetails : undefined,
  };
}

export interface PmRangeErrorDetails {
  /** `range_too_wide` / `out_of_retention` */
  reason?: string;
  maxDays?: number;
  retentionDays?: number;
  queryableRange?: { from: string | null; to: string | null };
}

/** 4004 的 `error_details`（其他错误码返回空对象） */
export function readRangeErrorDetails(error: unknown): PmRangeErrorDetails {
  const { bizCode, errorDetails: details } = readErrorCode(error);
  if (bizCode !== 4004 || !details) return {};
  const range = details.queryable_range as { from?: unknown; to?: unknown } | undefined;
  return {
    reason: typeof details.reason === 'string' ? details.reason : undefined,
    maxDays: typeof details.max_days === 'number' ? details.max_days : undefined,
    retentionDays: typeof details.retention_days === 'number' ? details.retention_days : undefined,
    queryableRange: range
      ? {
          from: typeof range.from === 'string' ? range.from : null,
          to: typeof range.to === 'string' ? range.to : null,
        }
      : undefined,
  };
}

export function classifyMonitorError(error: unknown): PmErrorKind {
  const { status, bizCode, errorDetails } = readErrorCode(error);
  if (bizCode === 4004) {
    return errorDetails?.reason === 'out_of_retention' ? 'outOfRetention' : 'rangeTooLong';
  }
  if (bizCode === 4100) return 'unavailable';
  if (bizCode === 3006 || status === 403) return 'forbidden';
  if (bizCode === 5001 || status === 503) return 'upstream';
  return 'unknown';
}

/**
 * 「记录 / 对象不存在」（1002）。
 *
 * 与真正的失败分开：查不到是**确定性**结果，给重试按钮只会让人反复点同一个必然失败的请求。
 */
export function isNotFound(error: unknown): boolean {
  return readErrorCode(error).bizCode === 1002;
}

/** 异常上的可读文案。非 Error（如字符串 reject）退成空串，由调用方决定兜底 */
export function readErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '';
}

export interface PmErrorTextKeys {
  /** i18n key：标题 */
  title: string;
  /** i18n key：说明。`null` = 用后端 message（仅 unknown 档） */
  description: string | null;
}

/** 分流结果 → i18n key。页面只消费这层，不在 JSX 里散落错误码判断 */
export function monitorErrorTextKeys(kind: PmErrorKind): PmErrorTextKeys {
  return {
    title: `platformMonitor.error.${kind}.title`,
    description: kind === 'unknown' ? null : `platformMonitor.error.${kind}.description`,
  };
}
