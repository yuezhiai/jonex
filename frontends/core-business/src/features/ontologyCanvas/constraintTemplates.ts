/**
 * `value_range` / `relation_cardinality` 的 min/max 在服务端 7 键形状里**没有承载字段** ——
 * 按拍板 ② 统一编码进 `expression` 固定模板：
 *
 *   value_range          → `范围：[<min>, <max>]`（`unbounded` ↔ 画布 `undefined`）
 *   relation_cardinality → `基数：[<min>, <max|unbounded>]`
 *
 * 读写两侧共用这两个编解码器 —— 画布「加载 → 不改 → 保存」的 round-trip（psql 逐字节相等）
 * 靠它们成立：数字 / `unbounded` / 字符串的写入形态与反解结果必须严格互逆。
 *
 * ⚠️ 非模板 `expression` 落在 value_range / relation_cardinality 档时会被**重写**成模板
 * （真机零此形态数据，见 spec §五）。`query_rule` / `required_attribute` / `unique_attribute`
 * 的 expression 不经这里。
 */

const UNBOUNDED = 'unbounded';

const formatBound = (value: number | string | undefined): string =>
  value === undefined || value === '' ? UNBOUNDED : String(value);

const toNumber = (text: string): number | undefined => {
  const value = Number(text);
  return Number.isFinite(value) ? value : undefined;
};

/** 画布 cardinality 不变量：整数、≥0、有限（非有限 → 0）。 */
const clampCardinality = (value: number): number =>
  Number.isFinite(value) ? Math.max(0, Math.trunc(value)) : 0;

/** 画布 value_range → 服务端 expression。`undefined` 一律写成 `unbounded`。 */
export function encodeRange(min: number | string | undefined, max: number | string | undefined): string {
  return `范围：[${formatBound(min)}, ${formatBound(max)}]`;
}

/** 画布 relation_cardinality → 服务端 expression。`'unbounded'` 原样写；min 按画布不变量归一（整数、≥0、非有限→0）；max 非有限归 `unbounded`（最宽上界，与 decode 兜底同向）。 */
export function encodeCardinality(min: number, max: number | 'unbounded'): string {
  const normalizedMax = max === 'unbounded' || !Number.isFinite(max) ? UNBOUNDED : String(clampCardinality(max));
  return `基数：[${clampCardinality(min)}, ${normalizedMax}]`;
}

const RANGE_PATTERN = /^范围：\[(.+), (.+)\]$/;
const CARDINALITY_PATTERN = /^基数：\[(.+), (.+)\]$/;

/** 服务端 expression → 画布 value_range 的 min/max。非模板 → 两个键都不产出（= unbounded）。 */
export function decodeRange(expression: string): { min?: number | string; max?: number | string } {
  const matched = RANGE_PATTERN.exec(expression.trim());
  if (!matched) return {};
  const bound = (text: string): number | string | undefined =>
    text === UNBOUNDED ? undefined : (toNumber(text) ?? text);
  const min = bound(matched[1]!);
  const max = bound(matched[2]!);
  return { ...(min !== undefined ? { min } : {}), ...(max !== undefined ? { max } : {}) };
}

/** 服务端 expression → 画布 relation_cardinality 的 min/max。非模板 → `0 / 'unbounded'`（记不住，但合法）。模板内解析不出的 bound 按 0/unbounded 兜底（画布类型装不下字符串）。 */
export function decodeCardinality(expression: string): { min: number; max: number | 'unbounded' } {
  const matched = CARDINALITY_PATTERN.exec(expression.trim());
  if (!matched) return { min: 0, max: UNBOUNDED };
  const rawMin = matched[1] === UNBOUNDED ? undefined : toNumber(matched[1]!);
  const min = rawMin === undefined ? 0 : Math.max(0, Math.trunc(rawMin));
  if (matched[2] === UNBOUNDED) return { min, max: UNBOUNDED };
  const rawMax = toNumber(matched[2]!);
  return { min, max: rawMax === undefined ? UNBOUNDED : Math.max(0, Math.trunc(rawMax)) };
}
