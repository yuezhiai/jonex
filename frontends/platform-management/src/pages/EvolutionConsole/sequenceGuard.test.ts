import { describe, expect, it } from 'vitest';
import { createSequenceGuard } from './sequenceGuard';

describe('createSequenceGuard（loadVersions 竞态守卫）', () => {
  it('只有最新一次请求的响应算数', () => {
    const guard = createSequenceGuard();
    const first = guard.begin();
    const second = guard.begin();

    expect(guard.isLatest(first)).toBe(false);
    expect(guard.isLatest(second)).toBe(true);
  });

  it('请求完成后仍是当前代际；下一次请求让旧序号失效', () => {
    const guard = createSequenceGuard();
    const seq = guard.begin();
    expect(guard.isLatest(seq)).toBe(true);

    const next = guard.begin();
    expect(guard.isLatest(seq)).toBe(false);
    expect(guard.isLatest(next)).toBe(true);
  });
});
