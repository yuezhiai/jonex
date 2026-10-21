/**
 * 平台监控的时刻格式化。**所有时间串都在这里按平台时区（Asia/Shanghai）落地**。
 *
 * 为什么不能直接用 `dayjs(value).format()`：那按**浏览器本地时区**出字。任务族接口给的是
 * `+08:00`、调用 / Token 族给的是 UTC `Z`，同一张页面上两族数据混排；浏览器一旦不在东八区，
 * 「调用时间」与「任务时间」会各自偏移、且偏移量还不一样，跨族对时间就永远对不上。
 *
 * 时区常量只此一处：产品若改口径（如跟随租户配置），改这里即可，页面与取数层都不用动。
 */
import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import timezone from 'dayjs/plugin/timezone';

dayjs.extend(utc);
dayjs.extend(timezone);

/** 平台展示时区。与后端任务族 `+08:00` 的输出口径一致 */
export const PM_TIMEZONE = 'Asia/Shanghai';

/** 页面上用到的几档格式，集中取名避免各处各写一遍 pattern */
export const PM_TIME_PATTERN = {
  /** 趋势横轴：小时桶 */
  bucketHour: 'HH:mm',
  /** 趋势横轴：天桶 */
  bucketDay: 'MM-DD',
  /** 列表里的时间（失败任务、调用记录、最近调用） */
  minute: 'MM-DD HH:mm',
  /** 需要秒的场合（对象最近调用） */
  second: 'MM-DD HH:mm:ss',
  /** 完整日期时间（模型用量最近调用） */
  full: 'YYYY-MM-DD HH:mm:ss',
  /** 重试明细：各 attempt 之间常只差几百毫秒，必须带毫秒 */
  millisecond: 'MM-DD HH:mm:ss.SSS',
  /** 工具条的「数据更新于」只看时钟 */
  clock: 'HH:mm:ss',
} as const;

/**
 * 接口时间串 → 平台时区下的展示串。空值与非法串一律返回 null（由调用方决定落成 `-`）。
 *
 * 接口给的都是带偏移的 ISO8601，`dayjs` 解析的是**瞬时**，再 `.tz()` 换到平台时区，
 * 与浏览器时区无关。
 */
export function fmtPmTime(value: string | null | undefined, pattern: string): string | null {
  if (!value) return null;
  const parsed = dayjs(value);
  if (!parsed.isValid()) return null;
  return parsed.tz(PM_TIMEZONE).format(pattern);
}

/** 本地 `Date`（取数时刻这类瞬时）→ 平台时区展示串。与 `fmtPmTime` 同一时区口径 */
export function fmtPmInstant(value: Date, pattern: string): string {
  return dayjs(value).tz(PM_TIMEZONE).format(pattern);
}
