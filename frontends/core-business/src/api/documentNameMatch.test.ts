import { describe, expect, it } from 'vitest';
import { pickExactNameMatch } from './domainKnowledge';

const rows = [
  { id: 'a', file_name: '设备维修样本数据.txt', updated_at: '2026-10-09T10:00:00Z' },
  { id: 'b', file_name: '设备维修样本数据.txt', updated_at: '2026-10-23T08:00:00Z' },
  { id: 'c', file_name: '设备维修样本数据-备份.txt', updated_at: '2026-10-24T08:00:00Z' },
];

describe('pickExactNameMatch', () => {
  it('只认完全同名并按更新时间取最新', () => {
    expect(pickExactNameMatch(rows, '设备维修样本数据.txt')?.id).toBe('b');
  });

  it('模糊命中但不同名不算同名', () => {
    expect(pickExactNameMatch(rows, '设备维修样本数据')).toBeNull();
  });

  it('无候选返回 null', () => {
    expect(pickExactNameMatch([], 'x.txt')).toBeNull();
  });
});
