import { describe, expect, it } from 'vitest';

import {
  anchorTables,
  buildLineageGraph,
  lineageEdgeId,
  lineageEdgeWidth,
  lineageRevision,
  type DataAssetEdge,
  type DataAssetGraph,
  type DataAssetNode,
  type LineageGraphModel,
} from './lineageGraphModel.js';

/** 测试节点：默认无属性（`attributeCount` 为 0）。 */
function node(table: string, extra: Partial<DataAssetNode> = {}): DataAssetNode {
  return { table, attributes: [], ...extra };
}

/** 测试边；`columns` 是 `[from_column, to_column]` 对，`origin`/`confidence` 缺省 = 不传。 */
function edge(
  from: string,
  to: string,
  columns: Array<[string, string]> = [],
  origin?: string,
  confidence?: number,
): DataAssetEdge {
  return {
    from_table: from,
    to_table: to,
    column_map: columns.map(([from_column, to_column]) => ({ from_column, to_column })),
    origin,
    confidence,
  };
}

function graph(
  nodes: DataAssetNode[],
  edges: DataAssetEdge[],
  extra: Partial<Pick<DataAssetGraph, 'unknown' | 'truncated'>> = {},
): DataAssetGraph {
  return { nodes, edges, unknown: false, truncated: false, ...extra };
}

function byId(model: LineageGraphModel, id: string): LineageGraphModel['nodes'][number] {
  const found = model.nodes.find((n) => n.id === id);
  if (!found) throw new Error(`节点不存在：${id}`);
  return found;
}

function edgeById(model: LineageGraphModel, id: string): LineageGraphModel['edges'][number] {
  const found = model.edges.find((e) => e.id === id);
  if (!found) throw new Error(`边不存在：${id}`);
  return found;
}

describe('anchorTables——画布锚点候选', () => {
  it('from/to 两端都收，去重后按码点排序（与边的返回顺序无关）', () => {
    const tables = anchorTables([
      { from_table: 'dbo.b', to_table: 'dbo.a' },
      { from_table: 'dbo.a', to_table: 'dbo.c' },
      { from_table: 'dbo.c', to_table: 'dbo.b' },
    ]);

    expect(tables).toEqual(['dbo.a', 'dbo.b', 'dbo.c']);
    // 输入顺序不影响输出顺序
    expect(
      anchorTables([
        { from_table: 'dbo.c', to_table: 'dbo.b' },
        { from_table: 'dbo.b', to_table: 'dbo.a' },
        { from_table: 'dbo.a', to_table: 'dbo.c' },
      ]),
    ).toEqual(tables);
  });

  it('没有边 → 空数组（父级据此显示「先有血缘边」空态）', () => {
    expect(anchorTables([])).toEqual([]);
  });

  it('自环表只出现一次', () => {
    expect(anchorTables([{ from_table: 'loop', to_table: 'loop' }])).toEqual(['loop']);
  });
});

describe('lineageRevision——画布重查的边集修订号', () => {
  it('表集合不变、边数变化 → 修订号变化（同一表对新增第二条列级边）', () => {
    const before = lineageRevision([{ from_table: 'a', to_table: 'b' }]);
    const after = lineageRevision([
      { from_table: 'a', to_table: 'b' },
      { from_table: 'a', to_table: 'b' },
    ]);

    expect(after).not.toBe(before);
    // 表集相同这一点单独成立：重查**不能**只靠表集
    expect(anchorTables([{ from_table: 'a', to_table: 'b' }])).toEqual(
      anchorTables([
        { from_table: 'a', to_table: 'b' },
        { from_table: 'a', to_table: 'b' },
      ]),
    );
    expect(before).toBe('1::a|b');
    expect(after).toBe('2::a|b');
  });

  it('时间戳取 updated_at ?? created_at 的最大值；缺失记空串', () => {
    const revision = lineageRevision([
      { from_table: 'a', to_table: 'b', created_at: '2026-10-01T00:00:00Z' },
      {
        from_table: 'b',
        to_table: 'c',
        created_at: '2026-09-01T00:00:00Z',
        updated_at: '2026-10-02T00:00:00Z',
      },
    ]);

    expect(revision).toBe('2:2026-10-02T00:00:00Z:a|b|c');
  });
});

describe('lineageEdgeWidth——置信度视觉编码', () => {
  it('置信度越高线越粗：缺省/0 → 1、0.5 → 2、1 → 3；负值下限 1', () => {
    expect(lineageEdgeWidth(undefined)).toBe(1);
    expect(lineageEdgeWidth(0)).toBe(1);
    expect(lineageEdgeWidth(0.5)).toBe(2);
    expect(lineageEdgeWidth(1)).toBe(3);
    expect(lineageEdgeWidth(-5)).toBe(1);
  });
});

describe('lineageEdgeId——画布边 id 口径', () => {
  it('id 用数据流方向 `from->to`，与 buildLineageGraph 的边 id 一致', () => {
    expect(lineageEdgeId('a', 'b')).toBe('a->b');

    const model = buildLineageGraph(graph([node('a'), node('b')], [edge('a', 'b')]), 'a');
    expect(model.edges[0].id).toBe('a->b');
    expect(model.edges[0].id).toBe(lineageEdgeId('a', 'b'));
  });
});

describe('buildLineageGraph——锚点分层方向', () => {
  it('上游在左（负深度）、下游在右（正深度），x 按层递增', () => {
    // dim_region → fact → agg_daily，锚点 fact
    const model = buildLineageGraph(
      graph(
        [
          node('fact'),
          node('dim_region', { entity_name: '区域', attributes: [{}, {}] }),
          node('agg_daily'),
        ],
        [edge('dim_region', 'fact'), edge('fact', 'agg_daily')],
      ),
      'fact',
    );

    const upstream = byId(model, 'dim_region');
    const anchor = byId(model, 'fact');
    const downstream = byId(model, 'agg_daily');

    expect(upstream.data.depth).toBe(-1);
    expect(anchor.data.depth).toBe(0);
    expect(downstream.data.depth).toBe(1);

    expect(upstream.position.x).toBeLessThan(anchor.position.x);
    expect(anchor.position.x).toBeLessThan(downstream.position.x);

    // minDepth = -1 → 归零后每层 300（不是让锚点固定 x=0）
    expect(upstream.position.x).toBe(0);
    expect(anchor.position.x).toBe(300);
    expect(downstream.position.x).toBe(600);
    // 每层各自从 y=0 起
    expect(upstream.position.y).toBe(0);
    expect(anchor.position.y).toBe(0);
    expect(downstream.position.y).toBe(0);

    // RF 节点形状：id=table，type=lineageTable，data 带展示字段
    expect(anchor.id).toBe('fact');
    expect(anchor.type).toBe('lineageTable');
    expect(anchor.data).toMatchObject({ table: 'fact', entityName: undefined, attributeCount: 0 });
    expect(upstream.data.entityName).toBe('区域');
    expect(upstream.data.attributeCount).toBe(2);
  });

  it('锚点存在但没有任何边 → 单节点、unknown=false', () => {
    const model = buildLineageGraph(graph([node('solo')], []), 'solo');

    expect(model.nodes).toHaveLength(1);
    expect(model.nodes[0]).toMatchObject({ id: 'solo', position: { x: 0, y: 0 } });
    expect(model.unknown).toBe(false);
  });
});

describe('buildLineageGraph——多跳深度（边顺序无关）', () => {
  it('下游 2 跳：边按子先序给出（b→c 在 a→b 前），c 仍为 +2', () => {
    // 边列表故意反向：单遍传播只能到达 b，必须多遍才能把深度推到 c。
    const model = buildLineageGraph(
      graph([node('a'), node('b'), node('c')], [edge('b', 'c'), edge('a', 'b')]),
      'a',
    );

    expect(byId(model, 'a').data.depth).toBe(0);
    expect(byId(model, 'b').data.depth).toBe(1);
    expect(byId(model, 'c').data.depth).toBe(2);
    // minDepth = 0 → 每层 300
    expect(byId(model, 'c').position.x).toBe(600);
    expect(model.nodes.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });

  it('上游 2 跳：边按子先序给出（c→b 在 b→a 前），c 仍为 -2', () => {
    const model = buildLineageGraph(
      graph([node('a'), node('b'), node('c')], [edge('c', 'b'), edge('b', 'a')]),
      'a',
    );

    expect(byId(model, 'a').data.depth).toBe(0);
    expect(byId(model, 'b').data.depth).toBe(-1);
    expect(byId(model, 'c').data.depth).toBe(-2);
    // minDepth = -2 → c=0、a=600
    expect(byId(model, 'c').position.x).toBe(0);
    expect(byId(model, 'a').position.x).toBe(600);
    expect(model.nodes.map((n) => n.id)).toEqual(['c', 'b', 'a']);
  });
});

describe('buildLineageGraph——深度是 BFS 而非逐边松弛（与边顺序无关）', () => {
  // 菱形：U→A、A→D、U→D，锚点 A。D 既是 A 的直连下游（+1），也能经 U→D
  // 到达（U=-1 ⇒ 0）。逐边松弛按边序会把 D 定成 0；BFS 必须恒为 +1。
  it.each([
    ['子先序', [edge('U', 'A'), edge('U', 'D'), edge('A', 'D')]],
    ['父先序', [edge('A', 'D'), edge('U', 'A'), edge('U', 'D')]],
  ])('菱形（%s）：U 恒为 -1、D 恒为 +1（直连下游，不是 0）', (_label, edges) => {
    const model = buildLineageGraph(graph([node('U'), node('A'), node('D')], edges), 'A');

    expect(byId(model, 'U').data.depth).toBe(-1);
    expect(byId(model, 'A').data.depth).toBe(0);
    expect(byId(model, 'D').data.depth).toBe(1);
  });

  // 星形 + 链：A→B1..Bn 是直连，Bn→Bn+1 只在链尾。逐边松弛若先走链边，
  // 会把直连的 B2/B3 叠成 +2/+3（交叉边把链深推到 k 跳开外）；BFS 下所有
  // 直连 Bi 恒为 +1、链尾 Bn+1 恒为 +2。
  const CHAIN_EDGES = (): DataAssetEdge[] => [
    edge('A', 'B1'),
    edge('A', 'B2'),
    edge('A', 'B3'),
    edge('B1', 'B2'),
    edge('B2', 'B3'),
    edge('B3', 'B4'),
  ];
  const chainNodes = () => ['A', 'B1', 'B2', 'B3', 'B4'].map((table) => node(table));

  it.each([
    ['直连在前', CHAIN_EDGES()],
    ['链边在前', [...CHAIN_EDGES().slice(3), ...CHAIN_EDGES().slice(0, 3)]],
    ['交错（旧松弛会逐层叠加）', [
      edge('A', 'B1'),
      edge('B1', 'B2'),
      edge('A', 'B2'),
      edge('B2', 'B3'),
      edge('A', 'B3'),
      edge('B3', 'B4'),
    ]],
    ['完全逆序', [...CHAIN_EDGES()].reverse()],
  ])('链不得把直连邻居推深（%s）：B1..B3 恒为 +1、B4 恒为 +2', (_label, edges) => {
    const model = buildLineageGraph(graph(chainNodes(), edges), 'A');

    for (const table of ['B1', 'B2', 'B3']) {
      expect(byId(model, table).data.depth).toBe(1);
    }
    expect(byId(model, 'B4').data.depth).toBe(2);
    // 布局也随深度稳定：B4 与直连层之间隔一层（x 相差 300）
    expect(byId(model, 'B4').position.x - byId(model, 'B1').position.x).toBe(300);
  });
});

describe('buildLineageGraph——同层布局', () => {
  it('同层按表名排序并纵向堆叠（y 每格 110）', () => {
    // 上游 b、c 乱序输入，锚点 a；同层必须按表名字典序排
    const model = buildLineageGraph(
      graph([node('a'), node('c'), node('b')], [edge('c', 'a'), edge('b', 'a')]),
      'a',
    );

    expect(byId(model, 'b').position).toEqual({ x: 0, y: 0 });
    expect(byId(model, 'c').position).toEqual({ x: 0, y: 110 });
    expect(byId(model, 'a').position).toEqual({ x: 300, y: 0 });
    // 输出顺序也确定：按深度升序，同层按表名
    expect(model.nodes.map((n) => n.id)).toEqual(['b', 'c', 'a']);
  });
});

describe('buildLineageGraph——环安全', () => {
  it('a→b、b→a 不振荡：重复构建结果一致，两节点深度稳定', () => {
    const data = graph([node('a'), node('b')], [edge('a', 'b'), edge('b', 'a')]);

    const first = buildLineageGraph(data, 'a');
    const second = buildLineageGraph(data, 'a');

    expect(second).toEqual(first);
    expect(byId(first, 'a').data.depth).toBe(0);
    expect(byId(first, 'b').data.depth).toBe(1);
    expect(first.edges.map((e) => e.id)).toEqual(['a->b', 'b->a']);
  });
});

describe('buildLineageGraph——悬空边', () => {
  it('端点不在 nodes 里的边被丢弃（节点/边上限独立生效的产物）', () => {
    const model = buildLineageGraph(
      graph(
        [node('a'), node('b')],
        [
          edge('a', 'b'),
          edge('b', 'ghost'), // to 端不在 nodes
          edge('phantom', 'a'), // from 端不在 nodes
        ],
      ),
      'a',
    );

    expect(model.nodes.map((n) => n.id)).toEqual(['a', 'b']);
    expect(model.edges.map((e) => e.id)).toEqual(['a->b']);
    // 悬空边不参与深度传播：b 的深度只由 a→b 决定
    expect(byId(model, 'b').data.depth).toBe(1);
  });

  it('与锚点不连通的节点与边不上画布', () => {
    const model = buildLineageGraph(
      graph([node('a'), node('b'), node('x'), node('y')], [edge('a', 'b'), edge('x', 'y')]),
      'a',
    );

    expect(model.nodes.map((n) => n.id)).toEqual(['a', 'b']);
    expect(model.edges.map((e) => e.id)).toEqual(['a->b']);
  });
});

describe('buildLineageGraph——unknown / truncated', () => {
  it('锚点不在 nodes 里 → 空图 + unknown=true，truncated 原样透传', () => {
    const model = buildLineageGraph(
      graph([node('a')], [], { truncated: true }),
      'not_projected',
    );

    expect(model.nodes).toEqual([]);
    expect(model.edges).toEqual([]);
    expect(model.unknown).toBe(true);
    expect(model.truncated).toBe(true);
  });

  it('data.unknown=true → 空图 + unknown=true（即使锚点存在）', () => {
    const model = buildLineageGraph(
      graph([node('a')], [edge('a', 'b')], { unknown: true, truncated: true }),
      'a',
    );

    expect(model.nodes).toEqual([]);
    expect(model.edges).toEqual([]);
    expect(model.unknown).toBe(true);
    expect(model.truncated).toBe(true);
  });

  it('正常图的 truncated 透传，unknown=false', () => {
    const model = buildLineageGraph(graph([node('a')], [], { truncated: true }), 'a');

    expect(model.nodes).toHaveLength(1);
    expect(model.unknown).toBe(false);
    expect(model.truncated).toBe(true);
  });
});

describe('buildLineageGraph——RF 边', () => {
  it('label = column_map 长度（字符串），data 带列数与列映射', () => {
    const columnMap = [
      { from_column: 'id', to_column: 'user_id' },
      { from_column: 'name', to_column: 'user_name' },
    ];
    const model = buildLineageGraph(
      graph(
        [node('a'), node('b'), node('c')],
        [
          edge('a', 'b', [
            ['id', 'user_id'],
            ['name', 'user_name'],
          ]),
          edge('a', 'c', []),
        ],
      ),
      'a',
    );

    const ab = edgeById(model, 'a->b');
    expect(ab.source).toBe('a');
    expect(ab.target).toBe('b');
    expect(ab.label).toBe('2');
    expect(ab.data?.columnCount).toBe(2);
    expect(ab.data?.columnMap).toEqual(columnMap);

    const ac = edgeById(model, 'a->c');
    expect(ac.label).toBe('0');
    expect(ac.data?.columnCount).toBe(0);
    expect(ac.data?.columnMap).toEqual([]);
  });

  it.each([
    ['declared', '#52c41a'],
    ['manual', '#1677ff'],
    ['comment', '#faad14'],
    ['platform', '#722ed1'],
    ['value', '#13c2c2'],
    ['derived', '#8c8c8c'],
  ])('origin=%s 的边色为 %s（stroke 与 label 颜色一致）', (origin, color) => {
    const model = buildLineageGraph(
      graph([node('a'), node('b')], [edge('a', 'b', [], origin)]),
      'a',
    );

    const ab = edgeById(model, 'a->b');
    expect(ab.style).toMatchObject({ stroke: color });
    expect(ab.labelStyle).toMatchObject({ fill: color });
  });

  it('置信度编码进边宽：高置信度更粗，缺省/0 保持 1', () => {
    const model = buildLineageGraph(
      graph(
        [node('a'), node('b'), node('c'), node('d')],
        [
          edge('a', 'b', [], 'manual', 1),
          edge('a', 'c', [], 'manual', 0.5),
          edge('a', 'd', [], 'manual'),
        ],
      ),
      'a',
    );

    expect(edgeById(model, 'a->b').style).toMatchObject({ strokeWidth: 3 });
    expect(edgeById(model, 'a->c').style).toMatchObject({ strokeWidth: 2 });
    expect(edgeById(model, 'a->d').style).toMatchObject({ strokeWidth: 1 });
  });

  it('未知 / 缺失 origin 一律灰色', () => {
    for (const origin of [undefined, 'brand_new_origin']) {
      const model = buildLineageGraph(
        graph([node('a'), node('b')], [edge('a', 'b', [], origin)]),
        'a',
      );
      expect(edgeById(model, 'a->b').style).toMatchObject({ stroke: '#8c8c8c' });
    }
  });
});
