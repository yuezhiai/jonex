/**
 * 单次检索 Token 用量的纯格式化逻辑（与组件拆开：便于单测 + 通过
 * react-refresh 的「文件只导出组件」约束）。
 */

/** 数值 → 单位后缀，按 10^3 递增；小于 1e3 不加后缀。 */
const UNIT_SUFFIX: Record<number, string> = {
  1e3: 'k',
  1e6: 'm',
  1e9: 'b',
};

/** 有效值判定：非有限数字 / 0 / 负数都视为「没有用量」。 */
function isValidUsage(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/**
 * 把 Token 总量压成短单位字符串（`999` / `1k` / `1.5k` / `181.9k` / `1.2m` / `1b`）。
 *
 * - 小于 1000 保持整数；其余取 k / m / b，保留 1 位小数并去掉无意义的 `.0`；
 * - 进位到 1000 时自动升一档（999,950 → `1m`，而不是 `1000k`）；
 * - 非有限数字 / 0 / 负数返回 `null`（= 不渲染）。
 *
 * @param value Token 总量（后端精确合计值）
 */
export function formatTokenUsage(value: number | null | undefined): string | null {
  if (!isValidUsage(value)) return null;

  const rounded = Math.round(value);
  if (rounded < 1e3) return String(rounded);

  let base = rounded >= 1e9 ? 1e9 : rounded >= 1e6 ? 1e6 : 1e3;
  let scaled = Math.round((rounded / base) * 10) / 10;
  // 999,950/1000 = 999.95 → 进位成 1000k 没意义，升到 m 档重算
  while (scaled >= 1000 && base < 1e9) {
    base *= 1e3;
    scaled = Math.round((rounded / base) * 10) / 10;
  }
  return `${scaled}${UNIT_SUFFIX[base]}`;
}

/**
 * 精确值（千分位），用于 `aria-label` 这类不需要压缩精度的地方。
 * 视觉上只显示短单位，读屏仍能拿到真实数字。
 *
 * @param value  Token 总量（后端精确合计值）
 * @param locale 可选的 BCP 47 locale（默认跟随运行环境；测试用来固定千分位行为）
 */
export function formatExactTokenUsage(
  value: number | null | undefined,
  locale?: string,
): string | null {
  if (!isValidUsage(value)) return null;
  return Math.round(value).toLocaleString(locale);
}
