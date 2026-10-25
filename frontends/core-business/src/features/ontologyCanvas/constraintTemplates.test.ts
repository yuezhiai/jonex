/**
 * min/max 的 expression 模板承载（拍板②：`范围：[0, 100]` / `基数：[0, unbounded]`）。
 * round-trip（psql 逐字节）成立的前提是编解码严格互逆：数字/`unbounded`/字符串
 * 三种形态写入与反解必须闭合。
 */
import { describe, expect, it } from 'vitest';

import { decodeCardinality, decodeRange, encodeCardinality, encodeRange } from './constraintTemplates.js';

describe('encodeRange / decodeRange', () => {
  it('数字往返（含小数——value_range 不做整数截断）', () => {
    expect(encodeRange(0, 100)).toBe('范围：[0, 100]');
    expect(decodeRange('范围：[0, 100]')).toEqual({ min: 0, max: 100 });
    expect(decodeRange(encodeRange(0.5, 99.9))).toEqual({ min: 0.5, max: 99.9 });
  });

  it('undefined ↔ unbounded（可逆）', () => {
    expect(encodeRange(undefined, undefined)).toBe('范围：[unbounded, unbounded]');
    expect(decodeRange('范围：[unbounded, unbounded]')).toEqual({});
    expect(encodeRange(0, undefined)).toBe('范围：[0, unbounded]');
    expect(decodeRange('范围：[0, unbounded]')).toEqual({ min: 0 });
  });

  it('非数字 bound 原样保留（字符串往返）', () => {
    expect(decodeRange('范围：[a, 5]')).toEqual({ min: 'a', max: 5 });
    expect(encodeRange('a', 5)).toBe('范围：[a, 5]');
  });

  it('非模板不识别 → 空对象', () => {
    expect(decodeRange('随便一段话')).toEqual({});
    expect(decodeRange('')).toEqual({});
  });

  it('encodeRange 空串 → unbounded（与 undefined 同档）', () => {
    expect(encodeRange('', 5)).toBe('范围：[unbounded, 5]');
  });
});

describe('encodeCardinality / decodeCardinality', () => {
  it('往返（数字 / unbounded）', () => {
    expect(encodeCardinality(0, 1)).toBe('基数：[0, 1]');
    expect(decodeCardinality('基数：[0, 1]')).toEqual({ min: 0, max: 1 });
    expect(decodeCardinality(encodeCardinality(1, 'unbounded'))).toEqual({ min: 1, max: 'unbounded' });
  });

  it('非模板 → 合法缺省 0 / unbounded', () => {
    expect(decodeCardinality('nonsense')).toEqual({ min: 0, max: 'unbounded' });
  });

  it('小数截断为整数（画布的 min/max 是整数）', () => {
    expect(decodeCardinality('基数：[1.9, 3.2]')).toEqual({ min: 1, max: 3 });
  });

  it('非规范输入归一（min 整数≥0、max 非有限→unbounded）', () => {
    expect(encodeCardinality(-1, 5)).toBe('基数：[0, 5]');
    expect(encodeCardinality(1.5, 3.2)).toBe('基数：[1, 3]');
    expect(encodeCardinality(0, Number.NaN)).toBe('基数：[0, unbounded]');
    expect(encodeCardinality(0, Number.POSITIVE_INFINITY)).toBe('基数：[0, unbounded]');
    expect(decodeCardinality(encodeCardinality(-1, 5))).toEqual({ min: 0, max: 5 });
  });

  it('cardinality 模板内垃圾 bound 的兜底：min→0、max→unbounded', () => {
    expect(decodeCardinality('基数：[a, 3]')).toEqual({ min: 0, max: 3 });
    expect(decodeCardinality('基数：[1, b]')).toEqual({ min: 1, max: 'unbounded' });
  });
});
