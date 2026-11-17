import dayjs from 'dayjs';

/**
 * 任务中心的时间与耗时格式化。单开一个文件是因为列表页与子任务组件都要用它们，
 * 塞在任一方里都会让另一方反向依赖。
 */

/**
 * 耗时格式化。接口给的是**整秒**且未结束时为空 —— 不在这里做「到现在为止」的推算，
 * 那是接口的职责（`duration_seconds` 的语义就是「没跑完就是空」）。
 */
export const fmtDuration = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
};

/**
 * Token 数格式化（2026-11-11 加，按文档归集）。
 *
 * `null` → `-`：口径是「算不出来 / 没有精确计量」，**不是 0**，绝不兜成 0；
 * 真有 `0` 就如实显示 `0`。千分位用 `toLocaleString('en-US')` —— 与调用记录那边
 * 同一视觉口径，不受浏览器 locale 影响（否则中文环境下可能被格式化成别的分隔符）。
 */
export const fmtTokenCount = (tokens: number | null | undefined): string => {
  if (tokens === null || tokens === undefined) return '-';
  return tokens.toLocaleString('en-US');
};

/**
 * ISO 8601 → 'YYYY-MM-DD HH:mm:ss'（本地时区）。
 *
 * 统一走 dayjs 而不是自己拼字符串：接口返回的时刻是否带时区尚未确认，而 dayjs 对
 * 带时区（`...Z` / `+08:00`）与不带时区两种输入都能正确解析成本地时刻。
 */
export const fmtDateTime = (iso: string | null | undefined): string =>
  iso ? dayjs(iso).format('YYYY-MM-DD HH:mm:ss') : '-';
