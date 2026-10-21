import type { TFunction } from 'i18next';

/**
 * 后端枚举码 → 展示文案。
 *
 * 这些码是**可增的**（后端加入新的调用方式 / 终态时前端不会同一天发版）。i18n 里查不到时
 * 直接展示原码：甩出 `platformMonitor.callProtocol.xxx` 这种键名，等于把内部实现漏给用户；
 * 展示成 `-` 又会被读成「没有值」，而实际上是有值、只是前端还不认识。
 *
 * 空码（null / undefined / 空串）才是真正的「没有值」，落成占位符。
 */
export function enumLabel(t: TFunction, prefix: string, code: string | null | undefined): string {
  if (!code) return '-';
  return t(`${prefix}.${code}`, { defaultValue: code });
}
