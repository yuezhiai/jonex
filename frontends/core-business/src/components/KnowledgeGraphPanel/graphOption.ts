/**
 * 图谱 → ECharts option 的纯映射层。
 *
 * 刻意不 import 任何运行时依赖（echarts 只取类型），这样它可以脱离浏览器单测，
 * 也让 index.tsx 只剩「实例生命周期 + 事件接线」。
 */
import type { EChartsOption } from 'echarts';
import type { OntologyGraphEdge, OntologyGraphNode } from '@/types/domainKnowledge';

// ── 类型色板 ────────────────────────────────────────────────────────────────
// 统一 S≈45% / L≈54%，只换色相。预设那组的毛病是明度不齐（#f59e0b 过亮、
// #8b5cf6 过暗、#ef4444 刺眼），明度拉平后就不再有哪个色抢眼。
// 注意别再往下压饱和度：画布上节点是小圆点且密集，靠色相区分类型，
// 饱和度压到 30% 时 12 个色相会挤成同一片灰蓝。
// 顺序按色相跳跃排布：类型按计数降序取色，相邻色相排一起会让相邻类型几乎同色
export const COLOR_PALETTE = [
  '#559bbe', // 湖蓝
  '#be7855', // 陶土
  '#55be9b', // 青绿
  '#9255be', // 雾紫
  '#be9b55', // 沙
  '#555ebe', // 雾靛
  '#be558a', // 藕荷
  '#9bbe55', // 橄榄
  '#5581be', // 雾蓝
  '#be55be', // 洋红
  '#be5e55', // 陶红
  '#6f55be', // 蓝紫
];

/** 本体抽取没识别出类型的实体（兜底分类 unknown）在真实数据里占比过半，
 *  让它占一个彩色色位的话，整张图会被染成同一种颜色、其余类型全被压掉。
 *  给中性灰：让「未识别」退成背景，彩色才重新成为信息。 */
export const UNKNOWN_TYPE_COLOR = '#a8b6c4';

/** 本体抽取没识别出类型的兜底分类，大小写与空值一并归入。
 *  注意：只影响**取色**，不影响 category 的名字——legend 的名字会原样回传给
 *  后端的 entity_types 参数，归一化过的名字后端认不出来。 */
export const isUnknownType = (type: string) => !type || type.toLowerCase() === 'unknown';

// ── 规模阈值 ────────────────────────────────────────────────────────────────
export const NODE_LABEL_LIMIT = 500;
/** 边标签只在很小的图上才给。本体抽取产出的边绝大多数是同一个泛化标签
 *  （实测 219 条边里 181 条都是 `RELATES_TO`），铺满画布也读不出信息，反而让
 *  开关图例时成批文字进出、看着像在闪。密度一旦上来就整体关掉，不逐条判断 */
export const EDGE_LABEL_LIMIT = 60;
/** 标签密度开关：节点多到这个数以上就不再给每个节点打标签，只留枢纽。
 *  500 个标签必然糊成一片；而顺着高连接度的节点往下读，本来就是看图的方式。
 *  阈值按度数实测定的：500 节点里度数 ≥5 的约 140 个，铺开正好读得清 */
export const LABEL_CROWD_THRESHOLD = 300;
export const LABEL_MIN_DEGREE = 5;
export const LARGE_GRAPH_THRESHOLD = 200;
/** 单次拉取的节点上限，与后端 limit 参数一致 */
export const NODE_LIMIT = 500;

export const NODE_LABEL_MAX = 12;

export function truncateLabel(name: string): string {
  return name.length > NODE_LABEL_MAX ? name.slice(0, NODE_LABEL_MAX) + '…' : name;
}

// ── 输出类型 ────────────────────────────────────────────────────────────────
export interface GraphCategory {
  name: string;
  itemStyle: { color: string };
}

export interface GraphNodeItem {
  id: string;
  name: string;
  fullName: string;
  /** tooltip 用。ECharts 不认这两个字段，跟着 data 一起带 */
  description: string;
  aliases: string[];
  /** 连接度。ECharts 的 force.repulsion / edgeLength 按它线性插值 */
  value: number;
  category: number;
  symbolSize: number;
  label: { show: boolean };
  itemStyle: { color: string };
}

export interface GraphEdgeItem {
  source: string;
  target: string;
  label: { show: boolean; formatter: string };
}

export interface BuiltGraph {
  categories: GraphCategory[];
  nodes: GraphNodeItem[];
  edges: GraphEdgeItem[];
  /** 类型 → 颜色，详情面板的头像底色要用（与 category 同源） */
  colorByType: Record<string, string>;
}

/** 类型键 → 计数，用于给 category 排序 */
type TypeCounts = Record<string, number>;

export function buildCategories(nodes: OntologyGraphNode[], typeCounts: TypeCounts): GraphCategory[] {
  // category 的 name 用**原始类型字符串**：legend 的名字会原样回传给后端的
  // entity_types 参数，归一化过的名字后端认不出来
  const keys = new Set<string>();
  Object.keys(typeCounts).forEach((t) => keys.add(t));
  nodes.forEach((n) => keys.add(n.type));

  const counts = new Map<string, number>();
  Object.entries(typeCounts).forEach(([t, c]) => counts.set(t, c));
  nodes.forEach((n) => {
    if (!(n.type in typeCounts)) counts.set(n.type, (counts.get(n.type) || 0) + 1);
  });

  // 未识别的垫底，其余按计数降序、同计数按名字升序（顺序稳定，色位才不会跳）
  const byCount = (a: string, b: string) =>
    (counts.get(b) || 0) - (counts.get(a) || 0) || a.localeCompare(b);
  const known = [...keys].filter((k) => !isUnknownType(k)).sort(byCount);
  const unknown = [...keys].filter((k) => isUnknownType(k)).sort(byCount);

  // 色位只发给已识别的类型：unknown 占彩色色位的话整张图会被染成同一片灰蓝
  return [
    ...known.map((name, i) => ({ name, itemStyle: { color: COLOR_PALETTE[i % COLOR_PALETTE.length] } })),
    ...unknown.map((name) => ({ name, itemStyle: { color: UNKNOWN_TYPE_COLOR } })),
  ];
}

export const edgeKey = (e: OntologyGraphEdge): string =>
  `${e.source_type}:${e.source}__${e.label}__${e.target_type}:${e.target}`;

export function buildGraph(
  raw: { nodes: OntologyGraphNode[]; edges: OntologyGraphEdge[] },
  typeCounts: TypeCounts,
): BuiltGraph {
  const categories = buildCategories(raw.nodes, typeCounts);
  const categoryIdx = new Map(categories.map((c, i) => [c.name, i]));
  const colorByType: Record<string, string> = {};
  categories.forEach((c) => {
    colorByType[c.name] = c.itemStyle.color;
  });

  // 连接度：既用于节点尺寸，也是 force 斥力的插值依据
  const degree = new Map<string, number>();
  for (const e of raw.edges) {
    const s = `${e.source_type}:${e.source}`;
    const t = `${e.target_type}:${e.target}`;
    degree.set(s, (degree.get(s) || 0) + 1);
    degree.set(t, (degree.get(t) || 0) + 1);
  }

  const showNodeLabels = raw.nodes.length <= NODE_LABEL_LIMIT;
  const showEdgeLabels = raw.edges.length <= EDGE_LABEL_LIMIT;
  const minDegree = raw.nodes.length > LABEL_CROWD_THRESHOLD ? LABEL_MIN_DEGREE : 0;

  const seenNodes = new Set<string>();
  const nodes: GraphNodeItem[] = [];
  for (const n of raw.nodes) {
    if (seenNodes.has(n.id)) continue;
    seenNodes.add(n.id);
    const deg = degree.get(n.id) || 0;
    nodes.push({
      id: n.id,
      name: truncateLabel(n.name),
      fullName: n.name,
      description: n.description || '',
      aliases: Array.isArray(n.aliases) ? n.aliases : n.aliases ? [n.aliases] : [],
      // 显式取 0：省略 value 会让 ECharts 的 linearMap 落到区间中点，
      // 孤立点因此拿到强斥力，把包围盒撑大、主体被 autoFit 缩小
      value: deg,
      category: categoryIdx.get(n.type) ?? 0,
      // 引用它的文档越多画得越大，但要封顶：不封的话高被引的实体会涨成一块色斑，
      // 把邻域整个盖住。基数按本体图的密度定的——234 个节点铺在视口里，30px 直径
      // 就会连成一片实心色块（验收反馈：圆点小一点），压到 16~32 才留得出空隙
      symbolSize: 16 + Math.min((n.doc_ids?.length || 0) * 2, 16),
      // unknown 节点不给标签：它们占实体六成以上、名字多是噪音，
      // 一起铺在图上会把真正有类型的节点标签压得读不出来
      label: { show: showNodeLabels && deg >= minDegree && !isUnknownType(n.type) },
      itemStyle: { color: colorByType[n.type] || UNKNOWN_TYPE_COLOR },
    });
  }

  const seenEdges = new Set<string>();
  const edges: GraphEdgeItem[] = [];
  for (const e of raw.edges) {
    const k = edgeKey(e);
    if (seenEdges.has(k)) continue;
    seenEdges.add(k);
    edges.push({
      source: `${e.source_type}:${e.source}`,
      target: `${e.target_type}:${e.target}`,
      label: { show: showEdgeLabels, formatter: e.label },
    });
  }

  return { categories, nodes, edges, colorByType };
}

/**
 * 力导向参数。数值是调参起点，不是结论——G6 与 ECharts 的力模型不同，
 * 原值不可直接搬用，最终以真机目视结果为准。
 *
 * repulsion 按节点 value（=连接度）线性插值：孤立点给弱斥力，它们之间互推
 * 纯粹在撑大包围盒。注意 edgeLength 的区间在 ECharts 源码里被反转，
 * 高度数节点拿到的是 edgeLength[1]。
 */
export function forceParams(nodeCount: number): {
  repulsion: number | [number, number];
  edgeLength: number | [number, number];
} {
  if (nodeCount > LARGE_GRAPH_THRESHOLD) {
    return { repulsion: [30, 220], edgeLength: [50, 110] };
  }
  return { repulsion: 400, edgeLength: 120 };
}

/**
 * @param hiddenTypes 被类型筛选切掉的类型名。重建 option 时得回填成 legend 的
 *   `selected`——ECharts 每次 setOption 都会按 option 里的选中态重置图例，
 *   不回填的话「双击扩图」那次重建会把用户刚选的筛选清掉。
 */
export function buildGraphOption(built: BuiltGraph, hiddenTypes: string[] = []): EChartsOption {
  const force = forceParams(built.nodes.length);
  return {
    // legend 组件留着不画：类型筛选（categoryFilter 对 node data 就地过滤）挂在
    // legend 的选中态上，删掉组件整条筛选链就断了。UI 换成了工具条上的多选下拉，
    // 由 dispatchAction legendSelect/legendUnSelect 驱动，状态仍存在这里
    legend: [
      {
        show: false,
        data: built.categories.map((c) => c.name),
        selected: Object.fromEntries(built.categories.map((c) => [c.name, !hiddenTypes.includes(c.name)])),
      },
    ],
    tooltip: {
      trigger: 'item',
      confine: true,
      formatter: (p: any) => {
        // 只做节点：边上的 tooltip 意义不大，且会把画布糊住
        if (p?.dataType !== 'node') return '';
        const d = p.data || {};
        const rows: string[] = [`<div style="font-weight:600">${d.fullName || d.name || ''}</div>`];
        if (d.description) rows.push(`<div style="color:#64748b;margin-top:4px">${d.description}</div>`);
        if (d.aliases?.length) rows.push(`<div style="color:#94a3b8">别名：${d.aliases.join('、')}</div>`);
        return rows.join('');
      },
    },
    series: [
      {
        type: 'graph',
        // 每次 setOption 都按当前节点集重新收敛。图例筛选会就地过滤 node data
        // （`categoryFilter.js` 对 nodeData 调 `filterSelf`），所以点图例时留下的
        // 节点会重新铺满画布——这是 ECharts graph 的默认行为，不额外固化坐标
        layout: 'force',
        roam: true,
        roamTrigger: 'global',
        // min 定 0.5（参考代码的取值）会让「适配视图」对中大型图失效：234 节点的
        // 包围盒高 777px、可视区仅 382px，需要缩到 0.39 倍才装得下，被下限夹住后
        // 按钮点了等于没点。放到 0.1 给足余量，用户手动缩得太小也能自己缩回来
        scaleLimit: { min: 0.1, max: 8 },
        draggable: true,
        // 图例的悬浮联动默认开着，而它在我们这套「搜索压暗」下只有害：联动靠
        // dispatchAction({type:'highlight'}) 实现，而 ECharts 对每次 highDown dispatch
        // 都会先 allLeaveBlur（core/echarts.js），一 hover 图例就把整张图的压暗清空，
        // 看上去像「节点全亮了」。节点本来就靠颜色分类型，这个联动的信息量也不大
        legendHoverLink: false,
        // 动画交给力导向自己分帧跑（见下面的 force.layoutAnimation），元素补间关掉。
        // 这两者是互斥的：GraphSeries 覆写了 isAnimationEnabled()，里面写着
        // `!(layout === 'force' && force.layoutAnimation)`——布局一分帧，整个系列的
        // 元素动画就被强制关掉，所以这行写不写结果一样，留着只为与参考实现一致
        animation: false,
        categories: built.categories,
        data: built.nodes,
        edges: built.edges,
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
        // 曲线而非直线：密集区的直线会织成网格，且双向关系（A→B 与 B→A）完全重叠
        autoCurveness: true,
        // 布局分帧迭代：GraphView 每 16ms 走一步、每步重渲一次，friction 每步 ×0.992
        // 衰减到 0.01 为止。这就是「图慢慢散开落位」的来源；关掉它布局会同步一次到位，
        // 但也就没有这个过程了。注意它同时决定了元素补间能不能用（见上面 animation 那条）
        //
        // friction 既是每步的阻尼也是停止判据：步数 = log(0.01/初值)/log(0.992)，默认
        // 0.6 要 510 步、实测 234 节点的图约 9.4 秒才停，久了点。压到 0.15 是 337 步、
        // 实测约 6.9 秒（两次同值），且布局质量没有付出代价——重叠的节点对数与包围盒
        // 尺寸都和 0.6 持平（27 对 vs 27~30 对）。再往下压开始变挤：实测 0.08 约 5.4 秒，
        // 但重叠对数涨到 34
        force: { ...force, layoutAnimation: true, friction: 0.15 },
        label: {
          position: 'right',
          formatter: '{b}',
          fontSize: 11,
          color: '#475569',
          // 文字描白边（halo）：标签压在节点、边或别的标签上时仍能读出来
          textBorderColor: '#ffffff',
          textBorderWidth: 3,
        },
        // 只给样式：显示与否由 buildGraph 算进每条边的 label.show（见上面 showEdgeLabels）
        edgeLabel: {
          fontSize: 9,
          color: '#94a3b8',
        },
        // ECharts 的 graph force 没有碰撞力，节点可能叠；这条至少保证标签不糊在一起
        labelLayout: { hideOverlap: true },
        // 悬停只让节点自身高亮，不做整体压暗：`focus: 'adjacency'` 会把非邻接项按
        // blur 压到 12% 透明，浅色底透上来等于给整张图蒙了层白纱
        // 只关掉节点放大，**不要给 lineStyle**。强调态的线色与透明度都是 ECharts
        // 兜底生成的：强调样式「缺色」时它回落到「元素**当前**线色 +10%」
        // （util/states.js 的 createEmphasisDefaultState，缺色时还会优先取 select 态
        // 的颜色）。写死颜色等于关掉这条回落——悬停一条在聚焦集里的边会被它从加深色
        // 拽回死色；写死透明度则会让压暗的边一悬停就变实色。留给它兜底，
        // 各态的线就各自提亮各自的那点，幅度一致
        emphasis: {
          scale: false,
        },
        // slate-400。原先用 slate-300（#cbd5e1）在白底上太淡，219 条边铺开几乎看不出
        // 结构（验收反馈：线再深一点）。再深一档到 slate-500 会开始跟节点抢眼
        lineStyle: { color: '#94a3b8', width: 1, curveness: 0.1 },
        // 搜索命中的那些边单独加深一档（slate-500）：搜索时非聚焦的边被压到 0.08
        // 透明度，聚焦出来的这块骨架得能靠颜色自己立起来，不能和别处一个色。
        // 挂 select 态是因为另两个态都有主了——blur 表示「不在聚焦集」、emphasis
        // 表示「正在悬停」，而「属于这次命中」是第三种、且与悬停无关的状态；
        // ECharts 的状态合成里这三者是并列的（core/echarts.js 的 applyElementStates），
        // 一条边可以同时「在聚焦集里」和「被悬停」。谁进这个态由搜索代码写元素上的
        // `selected` 决定，见 index.tsx 的 applySearchFocus。
        // 不覆盖 select.itemStyle：那是给节点的，我们只标边
        select: {
          lineStyle: { color: '#64748b' },
        },
        itemStyle: { borderColor: '#fff', borderWidth: 1 },
        // 搜索聚焦的样子：目标与其上下级保持本色，其余压暗。这里只给样式，
        // 「谁被压暗」由搜索代码写到目标元素的 ecData.focus 上（见 index.tsx
        // 的 applySearchFocus）—— 不能用 series.emphasis.focus:'adjacency'，
        // 那条路对「悬停」同样生效，会连带把悬停压暗整图（用户已否过）。
        // 暗到什么程度是照对比度定的：节点 0.18 配浅色底，聚焦集在满屏灰雾里
        // 仍然是一块实色；再亮一点区分度就掉，再暗就是一团影子
        blur: {
          itemStyle: { opacity: 0.18 },
          label: { opacity: 0.15 },
          lineStyle: { opacity: 0.08 },
        },
      },
    ],
    // 关掉最后一个可见类型会让 ECharts 6.1.0 崩在缩略图上：空集的包围盒退化成
    // 0×0，View 的整体变换矩阵行列式算成 NaN，`matrixInvert` 的 `if (!det) return null`
    // 命中后把 null 交给了 `matrixCopy` → `null[0]`。崩点在 legendselectchanged 之前，
    // 应用层拦不住（6.1.0 是当前最新版，无补丁可升）。好在那一击的终态——画布空、
    // 图例全灰——本来就是「一个类型都不显示」该有的样子，再点任一图例或刷新即恢复。
    // 若日后不愿留这条 console 报错，删掉整块 thumbnail 配置即可。
    thumbnail: {
      show: true,
      width: '15%',
      height: '15%',
      right: 12,
      bottom: 12,
      windowStyle: {
        color: 'rgba(140, 212, 250, 0.5)',
        borderColor: 'rgba(30, 64, 175, 0.7)',
        opacity: 1,
      },
    },
  };
}
