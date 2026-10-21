/**
 * 平台监控页的纯展示格式化。数字一律从这里出，各页不再各写一遍 toLocaleString。
 *
 * 与 `pages/DataOperations/utils.ts` 的同名工具刻意分开、不复用：本页的大数缩写规则不同
 * （k 保留 1 位小数，M/B 保留 2 位），并到一处会让两边的口径互相牵制。
 */
import type { TFunction } from 'i18next';

/** 千分位整数：13,900 */
export const fmtCount = (n: number) => n.toLocaleString('en-US');

/** 两位小数的百分比：99.38% */
export const fmtPercent = (n: number) => `${n.toFixed(2)}%`;

/** 一位小数的百分比：96.7% */
export const fmtPercent1 = (n: number) => `${n.toFixed(1)}%`;

/**
 * 大数缩写：1.33B / 367.24M / 657.1k / 1,970。
 *
 * k 用 1 位小数、M 与 B 用 2 位 —— 这是照设计稿定的（1.33B、367.24M 对 657.1k、2.0k），
 * 不是随手取的统一位数，改成一致会与截图对不上。
 */
export function fmtCompact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return fmtCount(n);
}

/** 响应时间：86ms / 1,426ms。超过 1 秒也不换算成秒，与设计稿一致 */
export const fmtLatency = (ms: number) => `${fmtCount(ms)}ms`;

/**
 * 可空的百分比 / 响应时间。
 *
 * 调用监控的接口在「窗口内一次调用都没有」时给 null，此时渲染成 `0%` 是**错误表述**
 * （没有调用 ≠ 全部失败），统一落成占位符。
 */
export const fmtPercentOrDash = (n: number | null) => (n == null ? '-' : fmtPercent(n));
export const fmtLatencyOrDash = (ms: number | null) => (ms == null ? '-' : fmtLatency(ms));

/** 一位小数的百分比，可空。任务侧成功率统一走它（同 `fmtPercent1` 的位数） */
export const fmtPercent1OrDash = (n: number | null) => (n == null ? '-' : fmtPercent1(n));

/**
 * 秒级时长：3分16秒 / 42秒（英文 locale 下是 3m 16s / 42s）。
 *
 * 「分 / 秒」是文种差异，不是格式差异，所以整段交给 i18n —— 硬拼中文会让英文界面漏出中文。
 */
export function fmtDuration(sec: number, t: TFunction): string {
  const minutes = Math.floor(sec / 60);
  return minutes > 0
    ? t('platformMonitor.duration.minuteSecond', { m: minutes, s: sec % 60 })
    : t('platformMonitor.duration.second', { s: sec });
}

/** 可空的秒级时长。任务侧平均耗时统一走它 */
export const fmtDurationOrDash = (sec: number | null, t: TFunction) =>
  sec == null ? '-' : fmtDuration(sec, t);
