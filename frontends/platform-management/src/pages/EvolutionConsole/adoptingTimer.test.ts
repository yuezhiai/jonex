import { describe, expect, it } from 'vitest';
import { resolveAdoptingStart } from './adoptingTimer';

describe('resolveAdoptingStart', () => {
  const observed = 1_700_000_000_000;
  const stale = 1_699_000_000_000;

  it('pending 阶段的时间戳可能是上一次尝试留下的 → 用观察时刻', () => {
    expect(resolveAdoptingStart('pending', stale, observed)).toBe(observed);
  });

  it('bench_status 缺失 → 用观察时刻', () => {
    expect(resolveAdoptingStart(null, stale, observed)).toBe(observed);
  });

  it('parent_ran 及以后的时间戳属于本次尝试 → 用后端时间戳', () => {
    expect(resolveAdoptingStart('parent_ran', stale, observed)).toBe(stale);
    expect(resolveAdoptingStart('passed', stale, observed)).toBe(stale);
  });

  it('时间戳非法 → 回落观察时刻', () => {
    expect(resolveAdoptingStart('parent_ran', Number.NaN, observed)).toBe(observed);
  });

  it('白名单外的状态（如 queued）→ 不用 benchStartedAt', () => {
    // 白名单只认 parent_ran 及以后；未知/新状态可能仍是上一轮尝试留下的时间戳。
    expect(resolveAdoptingStart('queued', stale, observed)).toBe(observed);
    expect(resolveAdoptingStart('adopting', stale, observed)).toBe(observed);
  });
});
