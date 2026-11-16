/**
 * 数据运营模块的纯函数工具。
 *
 * 数字串一律经这三个 format 产出，各处不再各写一遍 toLocaleString 与小数位 ——
 * 常量层与取数层都从这里取，接后端后数字换成真实值，格式化仍然只有这一条路。
 */

export const fmtCount = (n: number) => n.toLocaleString('en-US');
export const fmtRate = (n: number) => `${n.toFixed(2)}%`;
export const fmtSigned = (n: number) => `+${n.toLocaleString('en-US')}`;

/**
 * 毫秒耗时 → 展示串。小于 1s 用整数毫秒，否则保留 1 位小数秒。
 * null / NaN 显示破折号（后端旧行 `total_ms` 可为 NULL，不能伪造成 0）。
 */
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return '—';
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/**
 * 列表问句截断：超过上限时保留前 `max` 个字符并补省略号。
 *
 * 只负责展示串，**原文由调用方放在 `title` 上**（hover 看全文）——
 * 与设计 §7「列表只显示问句截断」一致，完整内容只在抽屉里。
 */
export function truncateText(value: string | null | undefined, max: number): string {
  const text = value ?? '';
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/* ══════════════════════ tab 值 ══════════════════════ */

/**
 * 数据运营的四个 tab 是同一页面的不同区块，tab 值落在 URL query 上。
 * 取值校验放这里，避免散在页面与 tab 栏两处。
 */
export type DoTabKey = 'assets' | 'knowledge' | 'search' | 'nl2sql';

/** 顺序即 tab 栏顺序 */
export const DO_TAB_KEYS: readonly DoTabKey[] = ['assets', 'knowledge', 'search', 'nl2sql'];

/** 非法或缺省的 query 一律回落第一个 tab，避免手改 URL 后白屏 */
export function toDoTabKey(value: string | null): DoTabKey {
  return DO_TAB_KEYS.includes(value as DoTabKey) ? (value as DoTabKey) : DO_TAB_KEYS[0];
}
