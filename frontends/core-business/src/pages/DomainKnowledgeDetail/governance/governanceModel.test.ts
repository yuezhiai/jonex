import { describe, expect, it } from 'vitest';
import { LEVEL_ORDER, LEVEL_TAGS, levelRank, tagFor } from './governanceModel';

describe('governanceModel', () => {
  it('等级顺序固定', () => {
    expect(LEVEL_ORDER).toEqual(['public', 'internal', 'confidential', 'secret']);
    expect(levelRank('public')).toBe(0);
    expect(levelRank('secret')).toBe(3);
  });
  it('未知等级按 public', () => {
    expect(levelRank('weird')).toBe(0);
  });
  it('标签映射未知值不崩', () => {
    expect(LEVEL_TAGS.secret.color).toBe('red');
    expect(tagFor(LEVEL_TAGS, 'weird')).toEqual({ key: 'weird', color: 'default' });
  });
});
