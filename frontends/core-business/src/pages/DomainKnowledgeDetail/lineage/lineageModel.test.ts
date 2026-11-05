import { describe, expect, it } from 'vitest';

import type { LineageEdge } from '@/api/lineage';
import { filterByNode, nodeKey } from './lineageModel.js';

/** 由 `from → to`（`表.列` 全串）造一条边；其余字段用固定值。 */
function edge(from: string, to: string, id = `${from}->${to}`): LineageEdge {
  const [from_table, from_column] = from.split('.');
  const [to_table, to_column] = to.split('.');
  return {
    id,
    data_source_id: 'ds_1',
    from_table,
    from_column,
    to_table,
    to_column,
    origin: 'manual',
  };
}

describe('nodeKey', () => {
  it('列级：同一张表的不同列是不同节点', () => {
    expect(nodeKey('t1', 'a')).not.toBe(nodeKey('t1', 'b'));
    expect(nodeKey('t1', 'a')).toBe('t1.a');
  });
});

describe('filterByNode——上下游方向', () => {
  // 链：t0.x → t1.a → t2.b
  const chain = [edge('t0.x', 't1.a'), edge('t1.a', 't2.b')];

  it('up 命中 `to` 端：返回「谁流向该节点」', () => {
    expect(filterByNode(chain, 't1.a', 'up').map((e) => e.id)).toEqual(['t0.x->t1.a']);
  });

  it('down 命中 `from` 端：返回「该节点流向谁」', () => {
    expect(filterByNode(chain, 't1.a', 'down').map((e) => e.id)).toEqual(['t1.a->t2.b']);
  });

  it('反向断言：from=t1.a → to=t2.b 在 down=t1.a 里命中、在 up=t1.a 里不命中', () => {
    const e = edge('t1.a', 't2.b');
    expect(filterByNode([e], 't1.a', 'down')).toEqual([e]);
    expect(filterByNode([e], 't1.a', 'up')).toEqual([]);
  });

  it('列级筛选：同一张表的另一列不参与匹配', () => {
    // 边从 t1.a 出发；查 t1.b 的下游必须为空（只看表名就会误命中）
    expect(filterByNode([edge('t1.a', 't2.b')], 't1.b', 'down')).toEqual([]);
    expect(filterByNode([edge('t0.x', 't1.a')], nodeKey('t1', 'b'), 'up')).toEqual([]);
  });

  it('空输入给空数组', () => {
    expect(filterByNode([], 't1.a', 'up')).toEqual([]);
    expect(filterByNode([], 't1.a', 'down')).toEqual([]);
  });
});
