/**
 * 相似检测设置的纯函数。
 *
 * 阈值在 UI 中以百分数（50~99）展示，存储为 0.5~0.999 的小数；
 * KB 未单独设置时回落到平台全局默认值。
 */

export const SIMILARITY_GLOBAL_DEFAULT = 0.85;

export function effectiveThreshold(kb: number | null, globalDefault = SIMILARITY_GLOBAL_DEFAULT): number {
  return kb ?? globalDefault;
}

export function normalizeThresholdInput(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const value = Number(trimmed) / 100;
  if (!Number.isFinite(value) || value < 0.5 || value > 0.999) return null;
  return value;
}
