import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';

import type { LineagePath, LineagePathStep } from '@/api/ontologyLineagePaths';
import { lineageEdgeId } from './lineageGraphModel.js';
import {
  PATH_HIGHLIGHT_PALETTE,
  PATH_HIGHLIGHT_STROKE_WIDTH,
  applyPathHighlights,
  type PathHighlightModel,
} from './lineagePathModel.js';

/** 画布边 data 的最小形状（与 `lineageGraphModel.LineageEdgeData` 同构，足够验证「原字段保留」）。 */
interface TestEdgeData extends Record<string, unknown> {
  origin?: string;
  columnCount: number;
}

/** 测试边：id 用生产的共享 helper（数据流方向），带一条基础 style 以便验证「原样返回」。 */
function edge(from: string, to: string, data: Partial<TestEdgeData> = {}): Edge<TestEdgeData> {
  return {
    id: lineageEdgeId(from, to),
    source: from,
    target: to,
    label: '1',
    style: { stroke: '#52c41a', strokeWidth: 1 },
    data: { columnCount: 1, ...data },
  };
}

/** 测试 step；端点/列等缺省值可被 `extra` 覆盖（默认 lineage：画布边只画血缘）。 */
function step(from: string, to: string, extra: Partial<LineagePathStep> = {}): LineagePathStep {
  return {
    type: 'lineage',
    from_table: from,
    from_column: 'id',
    to_table: to,
    to_column: 'id',
    origin: 'declared',
    reversed: false,
    ...extra,
  };
}

function path(steps: LineagePathStep[], extra: Partial<LineagePath> = {}): LineagePath {
  return { score: 1, transfers: 0, hits_target: false, steps, ...extra };
}

function byId<D extends Record<string, unknown>>(
  model: PathHighlightModel<D>,
  id: string,
): PathHighlightModel<D>['edges'][number] {
  const found = model.edges.find((e) => e.id === id);
  if (!found) throw new Error(`边不存在：${id}`);
  return found;
}

describe('applyPathHighlights——单路径', () => {
  it('路径经过的边全部高亮：首个路径色、线宽 4、pathIndexes=[1]，原 data 字段保留', () => {
    const edges = [edge('a', 'b'), edge('b', 'c'), edge('x', 'y')];
    const model = applyPathHighlights(edges, [path([step('a', 'b'), step('b', 'c')])]);

    for (const id of ['a->b', 'b->c']) {
      const highlighted = byId(model, id);
      expect(highlighted.style).toMatchObject({
        stroke: PATH_HIGHLIGHT_PALETTE[0],
        strokeWidth: PATH_HIGHLIGHT_STROKE_WIDTH,
      });
      expect(highlighted.data?.pathIndexes).toEqual([1]);
      // 边本身的结构与 data 原字段都保留（高亮是叠加，不是替换）
      expect(highlighted.source).toBe(id.split('->')[0]);
      expect(highlighted.target).toBe(id.split('->')[1]);
      expect(highlighted.label).toBe('1');
      expect(highlighted.data?.columnCount).toBe(1);
    }

    // 未命中的边原样返回（同一对象引用）
    expect(model.edges[2]).toBe(edges[2]);
    expect(model.edges[2].style).toMatchObject({ stroke: '#52c41a', strokeWidth: 1 });

    // 输入不被修改（高亮边是浅拷贝）
    expect(edges[0].style).toEqual({ stroke: '#52c41a', strokeWidth: 1 });
    expect(edges[0].data).toEqual({ columnCount: 1 });
  });

  it('高亮线宽为 4：高于置信度编码上限 3，最高置信度边与高亮边不会同宽', () => {
    expect(PATH_HIGHLIGHT_STROKE_WIDTH).toBe(4);

    const model = applyPathHighlights([edge('a', 'b')], [path([step('a', 'b')])]);
    expect(byId(model, 'a->b').style?.strokeWidth).toBe(4);
  });
});

describe('applyPathHighlights——reversed step', () => {
  it('reversed 只标遍历方向：高亮仍落在数据流方向的 a->b 边，而不是 b->a', () => {
    // 两条方向相反的画布边同时存在，确保断言能区分「数据流方向」与「遍历方向」。
    const edges = [edge('a', 'b'), edge('b', 'a')];
    const model = applyPathHighlights(edges, [
      path([step('a', 'b', { reversed: true })]),
    ]);

    expect(byId(model, 'a->b').data?.pathIndexes).toEqual([1]);
    expect(byId(model, 'a->b').style).toMatchObject({ stroke: PATH_HIGHLIGHT_PALETTE[0] });

    // 遍历反向的 b->a 没有被高亮：原样返回（同一引用、原样式）。
    expect(model.edges[1]).toBe(edges[1]);
    expect(model.edges[1].style).toEqual({ stroke: '#52c41a', strokeWidth: 1 });
  });
});

describe('applyPathHighlights——多路径重叠', () => {
  it('重叠边取第一条路径的颜色，pathIndexes 按路径顺序累积', () => {
    const edges = [edge('a', 'b'), edge('b', 'c'), edge('c', 'd')];
    const model = applyPathHighlights(edges, [
      path([step('a', 'b')]),
      path([step('a', 'b'), step('b', 'c')]),
    ]);

    // 共享边：路径 1 的颜色 + 两条路径的序号
    const shared = byId(model, 'a->b');
    expect(shared.style).toMatchObject({
      stroke: PATH_HIGHLIGHT_PALETTE[0],
      strokeWidth: PATH_HIGHLIGHT_STROKE_WIDTH,
    });
    expect(shared.data?.pathIndexes).toEqual([1, 2]);

    // 只属于路径 2 的边：路径 2 的颜色
    const second = byId(model, 'b->c');
    expect(second.style).toMatchObject({
      stroke: PATH_HIGHLIGHT_PALETTE[1],
      strokeWidth: PATH_HIGHLIGHT_STROKE_WIDTH,
    });
    expect(second.data?.pathIndexes).toEqual([2]);

    // 谁也没经过的边不变
    expect(model.edges[2]).toBe(edges[2]);
  });

  it('同一路径重复经过同一条边：路径序号只记一次', () => {
    const model = applyPathHighlights(
      [edge('a', 'b')],
      [path([step('a', 'b'), step('a', 'b')])],
    );

    expect(byId(model, 'a->b').data?.pathIndexes).toEqual([1]);
  });

  it('路径数超过色板长度时颜色循环：第 6 条路径回到第 1 色', () => {
    const edges = [
      edge('a', 'b'),
      edge('b', 'c'),
      edge('c', 'd'),
      edge('d', 'e'),
      edge('e', 'f'),
      edge('f', 'g'),
    ];
    const paths = [
      path([step('a', 'b')]),
      path([step('b', 'c')]),
      path([step('c', 'd')]),
      path([step('d', 'e')]),
      path([step('e', 'f')]),
      path([step('f', 'g')]),
    ];
    const model = applyPathHighlights(edges, paths);

    expect(PATH_HIGHLIGHT_PALETTE).toHaveLength(5);
    expect(byId(model, 'f->g').style).toMatchObject({ stroke: PATH_HIGHLIGHT_PALETTE[0] });
    expect(byId(model, 'f->g').data?.pathIndexes).toEqual([6]);
  });
});

describe('applyPathHighlights——未知 step 端点', () => {
  it('画布上没有对应边的 step 被忽略：不报错、不高亮、路径清单仍保留该 step', () => {
    const edges = [edge('a', 'b')];
    const model = applyPathHighlights(edges, [
      path([step('a', 'b'), step('b', 'ghost'), step('phantom', 'c')]),
    ]);

    expect(byId(model, 'a->b').data?.pathIndexes).toEqual([1]);
    // 只有一条画布边 → 没有别的边被高亮
    expect(model.edges).toHaveLength(1);

    // 路径清单描述的是后端路径本身，未匹配的 step 照常列出（端点/列原样）
    expect(model.pathSummaries[0].steps.map((s) => `${s.fromTable}->${s.toTable}`)).toEqual([
      'a->b',
      'b->ghost',
      'phantom->c',
    ]);
  });
});

describe('applyPathHighlights——空路径', () => {
  it('paths=[] → 边原样（同一引用、同一样式），清单为空', () => {
    const edges = [edge('a', 'b'), edge('b', 'c')];
    const model = applyPathHighlights(edges, []);

    expect(model.edges).toHaveLength(2);
    expect(model.edges[0]).toBe(edges[0]);
    expect(model.edges[1]).toBe(edges[1]);
    expect(model.edges.map((e) => e.style)).toEqual(edges.map((e) => e.style));
    expect(model.pathSummaries).toEqual([]);
  });
});

describe('applyPathHighlights——路径清单', () => {
  it('index 从 1 起、按输入顺序；step 字段 snake → camel 原样映射', () => {
    const model = applyPathHighlights(
      [edge('a', 'b'), edge('b', 'c')],
      [
        path([step('a', 'b', {
          type: 'lineage',
          from_column: 'user_id',
          to_column: 'id',
          origin: 'value',
          reversed: true,
        })]),
        path([
          step('a', 'b', { type: 'join' }),
          step('b', 'c', { type: 'join' }),
        ]),
      ],
    );

    expect(model.pathSummaries).toHaveLength(2);
    expect(model.pathSummaries.map((s) => s.index)).toEqual([1, 2]);
    expect(model.pathSummaries[0].steps).toEqual([
      {
        fromTable: 'a',
        fromColumn: 'user_id',
        toTable: 'b',
        toColumn: 'id',
        type: 'lineage',
        origin: 'value',
        reversed: true,
      },
    ]);
    expect(model.pathSummaries[1].steps.map((s) => s.type)).toEqual(['join', 'join']);
    expect(model.pathSummaries[1].steps.map((s) => s.fromTable)).toEqual(['a', 'b']);
  });

  it('score / transfers / hitsTarget 原样透传', () => {
    const model = applyPathHighlights(
      [edge('a', 'b')],
      [
        path([step('a', 'b')], { score: 2.75, transfers: 2, hits_target: true }),
        path([], { score: 0.5, transfers: 0, hits_target: false }),
      ],
    );

    expect(model.pathSummaries[0]).toMatchObject({
      index: 1,
      score: 2.75,
      transfers: 2,
      hitsTarget: true,
    });
    expect(model.pathSummaries[1]).toMatchObject({
      index: 2,
      score: 0.5,
      transfers: 0,
      hitsTarget: false,
      steps: [],
    });
  });
});

describe('applyPathHighlights——同表对的 join step 不高亮', () => {
  it('join step 与画布血缘边同表对时不染色（清单仍保留该 step）', () => {
    const edges = [edge('a', 'b')];
    const model = applyPathHighlights(
      edges,
      [path([step('a', 'b', { type: 'join' })])],
    );

    // 边原样（同一引用）——join 边不在画布上，不能借表对「借道」高亮血缘边。
    expect(model.edges[0]).toBe(edges[0]);
    // 清单不改写：这是路径真实走过的一跳，只是画不出来。
    expect(model.pathSummaries[0].steps[0].type).toBe('join');
  });
});
