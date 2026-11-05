/**
 * 「数据资产对齐」纯逻辑回归。
 *
 * 钉死两件事：
 * - `groupByStatus`：固定组序（候选 → 已启用 → 已拒绝）、组内保序、空组不产出；
 *   未知状态**不丢行**，排在已知三态之后。
 * - `suggestionSummary`：计数原样带回；缺项 / NaN / 负数归 0（面板不能显示 NaN）。
 */
import { describe, expect, it } from 'vitest';

import { groupByStatus, suggestionSummary } from './dataAlignmentModel';
import type { OntologyDataAlignment } from '@/api/ontologyAlignment';

const row = (id: string, status: string): OntologyDataAlignment => ({
  id,
  doc_type: `Doc${id}`,
  data_source_id: 'ds_a',
  entity_name: `Entity${id}`,
  status: status as OntologyDataAlignment['status'],
});

describe('groupByStatus', () => {
  it('按 候选→已启用→已拒绝 固定顺序分组，组内保持入参顺序', () => {
    const groups = groupByStatus([
      row('1', 'rejected'),
      row('2', 'proposed'),
      row('3', 'active'),
      row('4', 'proposed'),
    ]);
    expect(groups.map((g) => g.status)).toEqual(['proposed', 'active', 'rejected']);
    expect(groups[0].items.map((i) => i.id)).toEqual(['2', '4']);
    expect(groups[1].items.map((i) => i.id)).toEqual(['3']);
    expect(groups[2].items.map((i) => i.id)).toEqual(['1']);
  });

  it('空组不出现；空输入给空数组', () => {
    expect(groupByStatus([])).toEqual([]);
    const groups = groupByStatus([row('1', 'active')]);
    expect(groups.map((g) => g.status)).toEqual(['active']);
    expect(groups[0].items.map((i) => i.id)).toEqual(['1']);
  });

  it('未知状态不丢行：排在已知三态之后、按首见顺序', () => {
    const groups = groupByStatus([
      row('1', 'mystery'),
      row('2', 'proposed'),
      row('3', 'legacy'),
      row('4', 'mystery'),
    ]);
    expect(groups.map((g) => g.status)).toEqual(['proposed', 'mystery', 'legacy']);
    expect(groups[1].items.map((i) => i.id)).toEqual(['1', '4']);
  });
});

describe('suggestionSummary', () => {
  it('原样带回三个计数', () => {
    expect(suggestionSummary({ created: 2, existing: 3, unresolved: 4 })).toEqual({
      created: 2,
      existing: 3,
      unresolved: 4,
    });
  });

  it('缺项 / NaN / 负数 / 小数归一到非负整数', () => {
    expect(suggestionSummary(null)).toEqual({ created: 0, existing: 0, unresolved: 0 });
    expect(suggestionSummary(undefined)).toEqual({ created: 0, existing: 0, unresolved: 0 });
    expect(suggestionSummary({})).toEqual({ created: 0, existing: 0, unresolved: 0 });
    expect(
      suggestionSummary({ created: Number.NaN, existing: -1, unresolved: 2.9 }),
    ).toEqual({ created: 0, existing: 0, unresolved: 2 });
  });
});
