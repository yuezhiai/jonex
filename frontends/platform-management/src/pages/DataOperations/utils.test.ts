import { describe, expect, it } from 'vitest';
import { fmtDuration, truncateText } from './utils';

describe('fmtDuration', () => {
  it('小于 1 秒用整数毫秒，其余保留一位小数秒', () => {
    expect(fmtDuration(123)).toBe('123 ms');
    expect(fmtDuration(999)).toBe('999 ms');
    expect(fmtDuration(1200)).toBe('1.2 s');
    expect(fmtDuration(60000)).toBe('60.0 s');
  });

  it('null / NaN 显示破折号，不伪造 0', () => {
    expect(fmtDuration(null)).toBe('—');
    expect(fmtDuration(undefined)).toBe('—');
    expect(fmtDuration(Number.NaN)).toBe('—');
  });
});

describe('truncateText', () => {
  it('空值返回空串', () => {
    expect(truncateText(null, 60)).toBe('');
    expect(truncateText(undefined, 60)).toBe('');
  });

  it('不超过上限时原样返回', () => {
    expect(truncateText('短问句', 60)).toBe('短问句');
    expect(truncateText('a'.repeat(60), 60)).toBe('a'.repeat(60));
  });

  it('超过上限时截断并加省略号；hover 原文由调用方放在 title 上', () => {
    const long = 'a'.repeat(61);
    expect(truncateText(long, 60)).toBe(`${'a'.repeat(60)}…`);
  });
});
