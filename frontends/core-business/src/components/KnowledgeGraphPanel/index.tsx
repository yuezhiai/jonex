import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Button, Tag, Space, Spin, Empty, Input, Select, message, Tooltip } from 'antd';
import {
  SearchOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
  ExpandAltOutlined,
  NodeExpandOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
// 深路径取 ecData：搜索聚焦要往目标元素上挂 focus，而 ECharts 只把它当内部实现
// 暴露（`getECData = makeInner()`，键名由 makeInner 生成，写不死）。
// 包没有 exports 限制，且 `echarts/core` 与 `lib/` 是同一份模块实例，
// 这里拿到的就是 GraphView 内部用的那个。
import { getECData } from 'echarts/lib/util/innerStore.js';
import { getOntologyGraph, expandOntologyNeighbors } from '@/api/domainKnowledge';
import type { OntologyGraphNode, OntologyGraphData } from '@/types/domainKnowledge';
import {
  NODE_LIMIT,
  buildGraph,
  buildGraphOption,
  edgeKey,
  type BuiltGraph,
  type GraphCategory,
} from './graphOption';

// 画布上下留给浮层的高度：图容器从这两条线之间开始，
// 自动布局出来的节点才不会跑到工具条、状态栏底下被压住。
// 上沿只在首帧用这个值，之后按顶栏实测高度覆盖（顶栏会折行，见 toolbarOffset）
const CANVAS_TOOLBAR_OFFSET = 60;
const CANVAS_STATUS_OFFSET = 48;

/** 画布可视区的高度下限（不含上下两条浮层）。右列详情不参与行高，高度由它兜底 */
const CANVAS_MIN_HEIGHT = 480 + CANVAS_TOOLBAR_OFFSET + CANVAS_STATUS_OFFSET;

// 底部提示是纯文字，画布渐变底 + 密集节点会让它糊掉，所以垫一层白底
const CANVAS_OVERLAY_STYLE: React.CSSProperties = {
  pointerEvents: 'auto',
  padding: '6px 8px',
  borderRadius: 8,
  background: 'rgba(255,255,255,0.9)',
  border: '1px solid #eef2f6',
  boxShadow: '0 2px 8px rgba(15,23,42,0.06)',
  backdropFilter: 'blur(4px)',
};

// 顶部几块并排一行，必须等高，否则看起来是台阶。
// 它们的内容本身就参差：Input 24px、Tag 24px、Button 28px（各自的行高与边框不同）。
// 定值取内容最大值 28 + padding 6×2 = 40。
// 用 minHeight 而非 height：将来某块内容真超过 40 时它自己长高，好过被裁掉。
const CANVAS_PILL_MIN_HEIGHT = 40;

// 顶部几块不垫底：里面的 Input / Button 各自带着白底与边框，
// 外面再套一层就是白底叠白底，画布上平白多出三块色块。
// 只有数量组是纯文字、自己没有底——它落在右上角那片空白上，靠布局位置兜住可读性。
const CANVAS_TOP_PILL_STYLE: React.CSSProperties = {
  pointerEvents: 'auto',
  padding: '6px 8px',
  minHeight: CANVAS_PILL_MIN_HEIGHT,
};

/** ECharts 内建 loading 遮罩，替掉原先手写的那层半透明白底 */
const loadingOpts = (text: string) => ({
  text,
  color: '#3b82f6',
  textColor: '#475569',
  maskColor: 'rgba(255,255,255,0.6)',
  fontSize: 13,
});

/** graphRoam 是 graph 系列运行时注册的动作，ECharts 没给 .d.ts 类型。
 *  注意 zoom 必须和 originX/originY 同时出现：缺了它们 ECharts 会把整体变换的
 *  平移分量算成 NaN，且之后任何 roam 都修不回来，只能重建 series。 */
type RoamPayload =
  | { dx: number; dy: number }
  | { zoom: number; dx?: number; dy?: number; originX: number; originY: number };

const roamAction = (chart: any, payload: RoamPayload) =>
  chart?.dispatchAction({ type: 'graphRoam', seriesIndex: 0, ...payload });

const seriesOf = (chart: any) => chart?.getModel?.()?.getSeriesByIndex?.(0);

/** 搜索命中后视野放大的倍数，与迁移前 G6 版的 `zoomTo(getZoom() * 2.5)` 对齐 */
const SEARCH_ZOOM = 2.5;

/** 节点布局的数据空间包围盒 `[minX, minY, maxX, maxY]`。
 *  不能拿坐标系的 `getBoundingRect()` 当代替：graph 的 View 坐标系下它返回的
 *  是**视口**矩形（源码注释：「历史原因保留此名」），拿它算适配倍率恒等于 1 */
function nodesBBox(chart: any): [number, number, number, number] | null {
  const data = seriesOf(chart)?.getData?.();
  if (!data || !data.count()) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < data.count(); i++) {
    const p = data.getItemLayout(i);
    if (!p || Number.isNaN(p[0]) || Number.isNaN(p[1])) continue;
    minX = Math.min(minX, p[0]);
    maxX = Math.max(maxX, p[0]);
    minY = Math.min(minY, p[1]);
    maxY = Math.max(maxY, p[1]);
  }
  return minX === Infinity ? null : [minX, minY, maxX, maxY];
}

/** 把全部节点装进视口。
 *
 *  为什么需要它：ECharts 的 graph 不会自动适配——View 的初始变换是按布局跑之前的
 *  坐标算的，收敛后的包围盒可以远超视口，节点于是画到 thumbnail 底下
 *  （它只压缩视口，不裁剪内容）。G6 的 `autoFit` 管的正是这件事。
 *
 *  两步走：先以视口中心为锚缩到目标倍率，再把包围盒中心平移到视口中心。
 *  一次载荷同时带 zoom 与 dx/dy 时 ECharts 是「先平移、后缩放」，平移量会跟着
 *  缩放一起放大，所以必须分两次发。
 *
 *  `ease` 是这一步走完全程的比例，1 为一次到位。跟随布局展开时传小值：目标倍率每帧
 *  都在变，一步到位就等于视口每帧跳一次，看着一顿一顿；只走差值的一个比例，视口才是
 *  连续地追上去。 */
/** 「把全部节点装进视口」所需的倍率。抽出来是因为搜索放大也要拿它当基准：
 *  两处各算各的会漂——搜索按当前倍率乘，点一次就再乘一次（见 focusNodeEased） */
function fitScaleOf(chart: any): number | null {
  const vr = seriesOf(chart)?.coordinateSystem?.getViewRect?.();
  const box = nodesBBox(chart);
  if (!vr || !box) return null;
  const [minX, minY, maxX, maxY] = box;
  const pad = 40;
  return Math.min(
    (vr.width - pad * 2) / Math.max(maxX - minX, 1),
    (vr.height - pad * 2) / Math.max(maxY - minY, 1),
  );
}

function fitChartToNodes(chart: any, ease = 1) {
  const cs = seriesOf(chart)?.coordinateSystem;
  const vr = cs?.getViewRect?.();
  const box = nodesBBox(chart);
  const target = fitScaleOf(chart);
  if (!vr || !box || target == null) return;
  const [minX, minY, maxX, maxY] = box;
  const cx = vr.x + vr.width / 2;
  const cy = vr.y + vr.height / 2;

  const cur = cs.getRoamTransform()[0] || 1;
  roamAction(chart, { zoom: (cur + (target - cur) * ease) / cur, originX: cx, originY: cy });

  const m = cs.getRoamTransform();
  const bcx = m[0] * ((minX + maxX) / 2) + m[4];
  const bcy = m[0] * ((minY + maxY) / 2) + m[5];
  roamAction(chart, { dx: (cx - bcx) * ease, dy: (cy - bcy) * ease });
}

/** 跟随适配：布局展开期间持续取景，落位后再取一次终态景收工。
 *
 *  布局是分帧跑的（`force.layoutAnimation`，见 graphOption.ts），234 节点实测要约 7 秒
 *  才收敛，包围盒高度从 448px 一路长到 ~800px。只做「落位后适配」的话这 7 秒里图一直是
 *  溢出视口的（roam 停在 1，800px 对 382px 的可视区），所以展开途中也得跟着取。
 *
 *  跟随必须逐帧走小步（见 `fitChartToNodes` 的 `ease`）：早先按 600ms 节流直接跳到位，
 *  两次之间视口纹丝不动，观感就是一顿一顿地跳。逐帧逼近既连续，也顺带把布局每步的抖动
 *  低通掉——视口跟得比包围盒本身还稳。
 *
 *  停下来的判据不能只看「连续两帧没动」——每步之间隔着 16ms 的 `setTimeout`，两帧同值
 *  太容易撞上，实测那样会在 t≈0 就误判落位，图停在中间态的取景上。改成要求安静满 1 秒。
 *
 *  返回一个停止函数：跟随期间每帧都会把视口拉回包围盒中心，用户此刻搜索或缩放出来的
 *  取景会被下一帧抹掉（实测在布局收敛前搜索，2.5 倍的放大一点没剩），所以用户一动视口
 *  就得让路，见组件里 `roam`。 */
const VIEW_EASE = 0.12;
const SETTLE_QUIET_MS = 1000;

function fitChartWhenSettled(chart: any): () => void {
  let stopped = false;
  let prev = '';
  let quietSince = 0;
  let frames = 0;
  const tick = () => {
    // 帧数兜底，免得万一布局不停就空转下去
    if (stopped || chart.isDisposed?.() || ++frames > 3600) return;
    const box = nodesBBox(chart);
    // 还没有节点（option 尚未渲染完）时再等一帧，别就此退出、再也不适配
    if (!box) {
      requestAnimationFrame(tick);
      return;
    }
    const sig = box.map((v) => Math.round(v)).join(',');
    const now = Date.now();
    if (sig !== prev) {
      prev = sig;
      quietSince = now;
    } else if (now - quietSince > SETTLE_QUIET_MS) {
      // 落位：一次到位取终态景，收工
      fitChartToNodes(chart);
      return;
    }
    fitChartToNodes(chart, VIEW_EASE);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return () => {
    stopped = true;
  };
}

const FOCUS_MS = 420;

/** 把第 idx 个节点缓动送到视口中心并放大 `SEARCH_ZOOM` 倍。
 *
 *  迁移前的 G6 版是 `focusElement(500ms)` + `zoomTo(500ms)`，视野是「飞」过去的；
 *  直接 dispatch 一步到位虽然结果一样，但两百多个节点的图上瞬移会让人丢掉方位感
 *  ——上一眼在图的这头，下一帧到了那头，反而更懵。
 *
 *  目标在开跑时就冻结：若每帧拿当前倍率再乘 2.5，会指数发散。
 *  基准是**适配倍率**而不是「当前倍率」——后者不止帧间发散，跨次也发散：
 *  每点一次搜索就在上次的结果上再乘一次，实测连点 4 次
 *  `0.389 → 0.972 → 2.43 → 6.07 → 8`（撞上 scaleLimit 上限），同一句话搜四遍视野
 *  会一路放大。以适配倍率为基准则幂等，点几次都落在同一处。
 *  缩放一律绕视口中心走（中心恒不动），平移量每帧按「目标偏移 − 当前偏移」补正，
 *  于是两步能合成一帧发，不必像 `fitChartToNodes` 那样分两次。
 *
 *  返回停止函数，与跟随循环共用 `stopFollowRef` 一个槽位：用户一动视口就交权。 */
function focusNodeEased(chart: any, idx: number, durationMs = FOCUS_MS): () => void {
  const cs = seriesOf(chart)?.coordinateSystem;
  const vr = cs?.getViewRect?.();
  const p = seriesOf(chart)?.getData?.()?.getItemLayout?.(idx);
  if (!vr || !p || Number.isNaN(p[0]) || Number.isNaN(p[1])) return () => {};
  const cx = vr.x + vr.width / 2;
  const cy = vr.y + vr.height / 2;

  const m = cs.getRoamTransform();
  const fromScale = m[0] || 1;
  const toScale = (fitScaleOf(chart) ?? fromScale) * SEARCH_ZOOM;
  // 屏幕坐标 = scale × 节点坐标 + offset，要让节点落在视口中心，offset 就得是这个
  const toOffX = cx - toScale * p[0];
  const toOffY = cy - toScale * p[1];
  const [fromOffX, fromOffY] = [m[4], m[5]];

  let stopped = false;
  const t0 = performance.now();
  const tick = () => {
    if (stopped || chart.isDisposed?.()) return;
    const k = Math.min(1, (performance.now() - t0) / durationMs);
    const e = 1 - (1 - k) ** 3; // easeOutCubic：起步快、收尾稳，像「飞过去」而不是匀速推
    const cur = cs.getRoamTransform();
    roamAction(chart, {
      zoom: (fromScale + (toScale - fromScale) * e) / (cur[0] || 1),
      originX: cx,
      originY: cy,
    });
    const now = cs.getRoamTransform();
    roamAction(chart, {
      dx: fromOffX + (toOffX - fromOffX) * e - now[4],
      dy: fromOffY + (toOffY - fromOffY) * e - now[5],
    });
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return () => {
    stopped = true;
  };
}

/** 把搜索聚焦写进 ECharts：目标与其上下级保持本色，其余压暗。
 *
 *  为什么绕这么一圈：ECharts 表达「一个子集突出、其余压暗」只有 `emphasis.focus`
 *  一条路，而它是**系列级**的——挂上之后「悬停」也会压暗整图，那条路用户已经否过
 *  （浅色底上像蒙了层白纱）。但真正决定谁被压暗的其实是元素身上的 `ecData.focus`：
 *  GraphView 在 render 时按数据项的 `emphasis.focus === 'adjacency'` 往元素上写一次
 *  `{ node, edge }` 邻接索引，`blurSeries` 再照着它把索引外的元素压暗。
 *  所以这里只给目标元素补挂这份索引，系列配置一个字不改——悬停到别的节点时
 *  它身上没有 focus，`blurSeries` 会直接 early-return，完全不受影响。
 *
 *  邻接集自己按数据算，不去够 GraphView 的 `node.getAdjacentDataIndices()`：
 *  出入边都收进来正好就是「上级 + 下级」，而这份 edges 我们手上本来就有。
 *  压暗之外还要把聚焦集里的边**加深**，那是另一套状态，见 markFocusEdges。 */

/** 给搜索命中的边打/摘 `selected` 标记：ECharts 只看元素上这个布尔决定它进不进
 *  `select` 态，样式在 graphOption 的 `series.select.lineStyle` 里。
 *  摘的动作不能省——ECharts 不记得上一批是谁，留着的话下次换词搜索时它们还是深的。 */
function markFocusEdges(chart: any, indices: number[], selected: boolean): void {
  const edgeData = seriesOf(chart)?.getData?.('edge');
  for (const i of indices) {
    // 一条边是个小组：线本体 + 两端箭头。只有真正挂了 select 态样式的子元素才需要
    // 这个标记，组本身没挂、标了不生效，所以顺手做个判据而不是写死取第一个子元素
    edgeData?.getItemGraphicEl?.(i)?.traverse?.((child: any) => {
      if (child.states?.select) child.selected = selected;
    });
  }
}

/** 返回清理函数；换词搜索前必须先把上一份摘掉，否则再悬停到旧目标上会莫名压暗一圈。 */
function applySearchFocus(chart: any, built: BuiltGraph, idx: number): () => void {
  const nodeId = built.nodes[idx]?.id;
  const el = seriesOf(chart)?.getData?.()?.getItemGraphicEl?.(idx);
  // 图例把该类型关掉时节点不在画布上，取不到元素——此时只挪视野，不压暗
  if (!nodeId || !el) return () => {};

  const idxById = new Map(built.nodes.map((n, i) => [n.id, i]));
  const focusNodes = [idx];
  const focusEdges: number[] = [];
  built.edges.forEach((e, i) => {
    if (e.source !== nodeId && e.target !== nodeId) return;
    focusEdges.push(i);
    const s = idxById.get(e.source);
    const t = idxById.get(e.target);
    if (s != null) focusNodes.push(s);
    if (t != null) focusNodes.push(t);
  });

  getECData(el).focus = { node: focusNodes, edge: focusEdges };
  markFocusEdges(chart, focusEdges, true);
  return () => {
    const cur = seriesOf(chart)?.getData?.()?.getItemGraphicEl?.(idx);
    if (cur) getECData(cur).focus = null;
    markFocusEdges(chart, focusEdges, false);
  };
}

interface KnowledgeGraphPanelProps {
  kbId: string;
  /** 按来源文档过滤图谱节点，不传则返回 KB 级全库图谱 */
  docId?: string;
  /** 去掉自带的卡面（白底 / 边框 / 圆角 / 阴影）。嵌在已有卡片的容器里时，
   *  这层壳会与父卡片叠成「卡中卡」；独立图谱页没有父卡片，靠它成卡片，不传 */
  bare?: boolean;
}

export default function KnowledgeGraphPanel({ kbId, docId, bare }: KnowledgeGraphPanelProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  // 顶栏浮在画布上，画布从它的下沿开始。宽度一窄顶栏就会折行（左侧搜索框、右侧数量
  // 与按钮分处两端，装不下就各占一行），写死的 60 会被第二行压住 legend，故实际量
  const [toolbarOffset, setToolbarOffset] = useState(CANVAS_TOOLBAR_OFFSET);
  const chartRef = useRef<import('echarts').ECharts | null>(null);
  /** 当前 option 的映射结果，节点点击/搜索要拿它回查颜色与全名 */
  const builtRef = useRef<BuiltGraph | null>(null);
  const currentDataRef = useRef<OntologyGraphData | null>(null);
  /** 被类型筛选切掉的类型名。筛选是纯前端的——切一下只让 ECharts 藏起该类型的
   *  节点，不重新拉数据。但重建 option 时 ECharts 会按 option 里的 selected
   *  重置选中态，所以得记下来回填（双击扩图就会重建一次） */
  const hiddenTypesRef = useRef<string[]>([]);
  /** 图表实例可能晚于 loading 状态建立（首帧时 chart 还是 null），用它把状态补挂上去 */
  const loadingRef = useRef(true);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [searchKeyword, setSearchKeyword] = useState('');
  const [meta, setMeta] = useState<{
    total_nodes: number;
    total_relations: number;
    returned_nodes: number;
    returned_edges: number;
    truncated: boolean;
  } | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [degradedReason, setDegradedReason] = useState('');
  /** 当前图里的类型。工具条上的筛选下拉拿它当选项（含配色圆点） */
  const [categories, setCategories] = useState<GraphCategory[]>([]);
  /** 筛选下拉选中的类型。**空集 = 不筛**：框空着就是全展示，这是筛选框的常规读法，
   *  也顺带避开了 ECharts 关掉最后一个图例条目时缩略图抛 null 那个缺陷 */
  const [pickedTypes, setPickedTypes] = useState<string[]>([]);

  // ── 命令式视图操作 ─────────────────────────────────────────────────────────

  /** 布局展开期间的跟随适配循环（见 fitChartWhenSettled），用户改视口时停掉 */
  const stopFollowRef = useRef<(() => void) | null>(null);

  const roam = (payload: RoamPayload) => {
    // 用户主动动了视口就交出控制权：跟随循环还在跑的话，它下一帧就把这次操作拉回去了
    stopFollowRef.current?.();
    stopFollowRef.current = null;
    roamAction(chartRef.current, payload);
  };

  /** 搜索聚焦的挂载点（见 applySearchFocus）。换词搜索与点空白都要先收掉上一份 */
  const focusClearRef = useRef<(() => void) | null>(null);
  /** 当前聚焦的目标 dataIndex，没有聚焦时为 null。ECharts 会在鼠标移出元素时
   *  自己把压暗清掉（见 renderGraph 的 mouseout 监听），留着它才好重放 */
  const focusIdxRef = useRef<number | null>(null);

  const clearSearchFocus = useCallback(() => {
    focusClearRef.current?.();
    focusClearRef.current = null;
    focusIdxRef.current = null;
    // 摘掉 focus 只影响「下次谁被压暗」，已经压暗的那些得靠 downplay 还原
    (chartRef.current as any)?.dispatchAction({ type: 'downplay', seriesIndex: 0 });
  }, []);

  /** View 坐标系没给公开访问器，只能走 getModel()。取的是**视口**矩形，
   *  它已经扣掉了图表组件占用的边距，跟容器尺寸不是一回事 */
  const viewInfo = (): { cx: number; cy: number; rect: [number, number, number, number] } | null => {
    const vr = (chartRef.current as any)?.getModel?.()?.getSeriesByIndex?.(0)?.coordinateSystem?.getViewRect?.();
    if (!vr) return null;
    return { cx: vr.x + vr.width / 2, cy: vr.y + vr.height / 2, rect: [vr.x, vr.y, vr.width, vr.height] };
  };

  const handleZoomIn = () => {
    const v = viewInfo();
    if (v) roam({ zoom: 1.3, originX: v.cx, originY: v.cy });
  };

  const handleZoomOut = () => {
    const v = viewInfo();
    if (v) roam({ zoom: 1 / 1.3, originX: v.cx, originY: v.cy });
  };

  const handleFitView = () => fitChartToNodes(chartRef.current);

  // ── 数据加载与渲染 ─────────────────────────────────────────────────────────

  // 展开邻居。定义必须早于 renderGraph —— 后者要把它接进双击回调
  const expandNode = useCallback(
    async (node: OntologyGraphNode) => {
      if (!kbId || !chartRef.current) return;
      setExpanding(true);
      try {
        const res = await expandOntologyNeighbors(kbId, node.type, node.name);
        if (res.degraded) {
          message.warning(res.degraded_reason || t('knowledgeGraph.dbUnavailable'));
          return;
        }
        const cur = currentDataRef.current;
        if (!cur) return;
        const existIds = new Set(cur.nodes.map((n) => n.id));
        const newNodes = res.nodes.filter((n) => {
          if (existIds.has(n.id)) return false;
          existIds.add(n.id);
          return true;
        });
        const existEdgeKeys = new Set(cur.edges.map(edgeKey));
        const newEdges = res.edges.filter((e) => {
          const k = edgeKey(e);
          if (existEdgeKeys.has(k)) return false;
          existEdgeKeys.add(k);
          return true;
        });
        if (newNodes.length === 0 && newEdges.length === 0) {
          message.info(t('knowledgeGraph.noMoreNeighbors'));
          return;
        }
        cur.nodes.push(...newNodes);
        cur.edges.push(...newEdges);

        const next = buildGraph(cur, cur.type_counts || {});
        builtRef.current = next;
        // 新节点入局后整张图重新收敛，老节点也会跟着漂——接受这一点，换来的是
        // 不必维护一套坐标固化逻辑（详见 buildGraphOption 里 layout 那条注释）
        chartRef.current.setOption(buildGraphOption(next, hiddenTypesRef.current));
        setMeta((m) => (m ? { ...m, returned_nodes: cur.nodes.length, returned_edges: cur.edges.length } : m));
        message.success(t('knowledgeGraph.expandCount', { newNodes: newNodes.length, newEdges: newEdges.length }));
      } catch (e: any) {
        message.error(e?.message || t('knowledgeGraph.expandFailed'));
      } finally {
        setExpanding(false);
      }
    },
    [kbId, t],
  );

  const onNodeClick = useCallback((params: any) => {
    if (params?.dataType !== 'node') return;
    const data = currentDataRef.current;
    const nodeId = params.data?.id;
    if (!data || !nodeId) return;
    const nodeData = data.nodes.find((n) => n.id === nodeId);
    if (!nodeData) return;

    const connectedEdges = data.edges.filter(
      (e) => `${e.source_type}:${e.source}` === nodeId || `${e.target_type}:${e.target}` === nodeId,
    );
    setSelectedNode({
      ...nodeData,
      connectedEdges,
      // 颜色现在由 category 统一决定，同一类型只有一个来源
      typeColor: builtRef.current?.colorByType[nodeData.type] || '#8b5cf6',
    });
  }, []);

  const onNodeDblClick = useCallback(
    (params: any) => {
      if (params?.dataType !== 'node') return;
      const nodeData = currentDataRef.current?.nodes.find((n) => n.id === params.data?.id);
      if (nodeData) void expandNode(nodeData);
    },
    [expandNode],
  );

  /** 筛选下拉 → legend 选中态。不重建 option：重建要走 setOption，那会让力导向
   *  整图重新收敛，而 dispatchAction 只切类别可见性（留下的节点重排是 ECharts 的既定行为） */
  const handleTypeFilterChange = useCallback(
    (next: string[]) => {
      const chart = chartRef.current;
      if (!chart) return;
      const all = categories.map((c) => c.name);
      const nextSet = new Set(next);
      // 空集代表「不筛」，落到 legend 上就是把每一类都选回来
      const visibleSet = next.length ? nextSet : new Set(all);
      const prevSet = new Set(pickedTypes.length ? pickedTypes : all);
      all.forEach((name) => {
        if (prevSet.has(name) === visibleSet.has(name)) return;
        chart.dispatchAction({ type: visibleSet.has(name) ? 'legendSelect' : 'legendUnSelect', name });
      });
      hiddenTypesRef.current = all.filter((n) => !visibleSet.has(n));
      // 按类型顺序存，标签顺序才不会跟着点击顺序跳
      setPickedTypes(all.filter((n) => nextSet.has(n)));
    },
    [categories, pickedTypes],
  );

  const renderGraph = useCallback(
    async (raw: OntologyGraphData) => {
      if (!containerRef.current) return;
      if (chartRef.current) {
        chartRef.current.dispose();
        chartRef.current = null;
      }
      // 旧实例连同它的元素一起没了，上一份 focus 清理函数不能再留——它闭包里抓的是
      // 已 dispose 的实例，再调就是往尸体上摸。目标索引同理，新图上那个下标指向的是别人
      focusClearRef.current = null;
      focusIdxRef.current = null;

      const built = buildGraph(raw, raw.type_counts || {});
      builtRef.current = built;
      setCategories(built.categories);
      // 回填筛选框：图例全开时框里就该是空的（空集 = 不筛）
      setPickedTypes(
        hiddenTypesRef.current.length
          ? built.categories.filter((c) => !hiddenTypesRef.current.includes(c.name)).map((c) => c.name)
          : [],
      );

      try {
        const echarts = (await import('./echartsSetup')).default;
        const chart = echarts.init(containerRef.current, undefined, { renderer: 'canvas' });
        chart.setOption(buildGraphOption(built, hiddenTypesRef.current));
        chart.on('click', onNodeClick);
        chart.on('dblclick', onNodeDblClick);
        chart.getZr().on('click', (e: any) => {
          // zrender 的 click 只有落空时才没有 target，用它区分「点空白」与「点节点」
          if (!e.target) {
            setSelectedNode(null);
            // 搜索聚焦会一直压暗大半个图，得留个出口：点空白即恢复
            clearSearchFocus();
          }
        });
        // 用户的原生手势不经过上面那个 roam 包装（ECharts 内部直接改变换），得在 zrender
        // 层听。不听的话，收敛的那 ~7 秒里用户拖走画布，跟随循环下一帧就把他拉回来——
        // 实测拖 180px 只有约 10px 存活，800ms 后几乎归位，等于这几秒内画布拖不动。
        //
        // 平移记的是「按住左键移动」而不是 mousedown：点节点看详情、点空白取消选中
        // 同样是 mousedown，可那一下视口一个字没动。那种情况必须让跟随继续跑完——
        // 跟随是唯一会在落位后做终态适配的人，早早收工就等于把图留在溢出的中间态
        const zr = chart.getZr();
        const yieldViewport = () => {
          stopFollowRef.current?.();
          stopFollowRef.current = null;
        };
        zr.on('mousewheel', yieldViewport);
        zr.on('touchstart', yieldViewport);
        zr.on('mousemove', (e: any) => {
          if (e?.event?.buttons & 1) yieldViewport();
        });
        // 搜索压暗会被 ECharts 自己清掉，它有两条路：
        //  ① `bindMouseEvent`（core/echarts.js）在 zrender 的 mouseout 里无条件调
        //     `allLeaveBlur(api)`，鼠标从**任一**元素移出就清空全图 blur。实测 hover
        //     一下搜索目标再移到空白，压暗元素 565 → 0，看着像「等一会自己好了」。
        //  ② `dispatchAction` 对 highDown（highlight/downplay）载荷也会先 allLeaveBlur
        //     （core/echarts.js:1514）——图例的悬浮联动就走这条。
        // 两处都发生在 chart.init / ECharts 内部动作里，早于我们注册的监听，所以都是
        // 它先清、我们后补。补的方式是重放 highlight：元素上的 `ecData.focus` 一直都在，
        // 被清掉的只是状态。`replaying` 挡住自己重放引起的那次事件，否则首尾相接地打转
        let replaying = false;
        const replayFocus = () => {
          const idx = focusIdxRef.current;
          if (idx == null || replaying) return;
          const el = seriesOf(chart)?.getData?.()?.getItemGraphicEl?.(idx);
          // 元素没了（图例恰好把该类型关掉）或 focus 已被摘就不要补了，补了反而压暗一片
          if (!el || !getECData(el).focus) return;
          replaying = true;
          chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx });
          replaying = false;
        };
        zr.on('mouseout', replayFocus);
        chart.on('highlight', replayFocus);
        chart.on('downplay', replayFocus);
        chartRef.current = chart;
        // 布局在分帧跑，这里量到的还不是终态，等它落位了再适配（见 fitChartWhenSettled）
        stopFollowRef.current?.();
        stopFollowRef.current = fitChartWhenSettled(chart);
        // 实例是在 loading 还没落回 false 时建的，遮罩得在这里补挂
        if (loadingRef.current) chart.showLoading('default', loadingOpts(t('knowledgeGraph.loadingGraph')));
      } catch (e: any) {
        setError(t('knowledgeGraph.graphRenderFailed', { message: e?.message || e }));
      }
    },
    [onNodeClick, onNodeDblClick, clearSearchFocus, t],
  );

  const loadData = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    setError(null);
    try {
      const graphData = await getOntologyGraph(kbId, {
        limit: NODE_LIMIT,
        documentId: docId || undefined,
      });
      currentDataRef.current = graphData;
      setDegraded(!!graphData.degraded);
      setDegradedReason(graphData.degraded_reason || '');
      setMeta({
        total_nodes: graphData.total_nodes,
        total_relations: graphData.total_relations,
        returned_nodes: graphData.returned_nodes,
        returned_edges: graphData.returned_edges,
        truncated: graphData.truncated,
      });

      await renderGraph(graphData);
    } catch (e: any) {
      setError(e?.message || t('knowledgeGraph.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, docId, renderGraph, t]);

  // 换知识库/换文档等于换了一张图，图例筛选跟着重置。点「刷新」则保留——那只是
  // 重拉同一张图的数据，顺手把用户刚点的筛选清掉反而意外
  useEffect(() => {
    hiddenTypesRef.current = [];
  }, [kbId, docId]);

  // 初始化和清理
  useEffect(() => {
    loadData();
    return () => {
      if (chartRef.current) {
        chartRef.current.dispose();
        chartRef.current = null;
      }
    };
  }, [loadData]);

  // 画布跟随容器：容器是绝对定位撑满的，窗口变窄时图不会跟着缩
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => chartRef.current?.resize());
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 顶栏折行与否要看实际渲染高度（数量提示出现、展开时的 Spin 都会改内容），
  // 量完把画布上沿推到它下面。容器尺寸一变上面那个 RO 会自己 resize 图
  useEffect(() => {
    const el = toolbarRef.current;
    if (!el) return;
    const measure = () => setToolbarOffset(el.offsetTop + el.offsetHeight + 8);
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    loadingRef.current = loading;
    const chart = chartRef.current;
    if (!chart) return;
    if (loading) chart.showLoading('default', loadingOpts(t('knowledgeGraph.loadingGraph')));
    else chart.hideLoading();
  }, [loading, t]);

  // 搜索节点：命中后标记并居中放大
  const handleSearch = () => {
    if (!searchKeyword.trim() || !currentDataRef.current || !chartRef.current) return;
    const kw = searchKeyword.toLowerCase();
    const idx =
      builtRef.current?.nodes.findIndex(
        (n) => n.fullName.toLowerCase().includes(kw) || n.id.toLowerCase().includes(kw),
      ) ?? -1;
    if (idx < 0) {
      message.info(t('knowledgeGraph.nodeNotFound'));
      return;
    }
    const chart = chartRef.current as any;
    const built = builtRef.current;
    if (!built) return;
    // 缓动聚焦（见 focusNodeEased）。占同一个槽位，跟随循环若还在跑先让位——
    // 否则它下一帧就把这趟取景拉回去了
    stopFollowRef.current?.();
    stopFollowRef.current = focusNodeEased(chart, idx);
    // 先收掉上一次的压暗，再按新目标重新聚焦
    clearSearchFocus();
    focusClearRef.current = applySearchFocus(chart, built, idx);
    focusIdxRef.current = idx;
    chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx });
  };

  return (
    <div
      style={{
        flex: 1,
        // 内边距与外层卡面解耦：bare 时卡面交给父卡片，但内容仍需要离边框的呼吸空间
        padding: 20,
        display: 'flex',
        // 画布与节点详情**并排**。详情原先垫在画布下沿，一展开就把画布压掉一截，
        // 而它自己还常常长得超出视口（关系多的节点尤甚），两边互相挤；
        // 拆成独立一列后画布尺寸恒定，详情也不用追着页面往下滚
        flexDirection: 'row',
        alignItems: 'stretch',
        gap: 12,
        overflowY: 'scroll',
        ...(bare
          ? {}
          : {
              background: '#fff',
              borderRadius: 10,
              border: '1px solid #eef2f6',
              boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            }),
      }}
    >
      {/* ECharts 画布 */}
      <div
        style={{
          flex: 1,
          borderRadius: 10,
          border: '1px solid #eef2f6',
          background: 'radial-gradient(ellipse at center, #f8fafc 0%, #f0f4f8 100%)',
          overflow: 'hidden',
          position: 'relative',
          // 加上上下浮层占的那两条，图谱可视区不小于原来的 480
          minHeight: CANVAS_MIN_HEIGHT,
        }}
      >
        {/* 工具条与状态栏浮在画布上：原先两者各占画布上下各一行，画布因此矮两截。
            外层不接事件，空白处仍能拖动/缩放画布；只有控件本身吃掉指针 */}
        <div
          ref={toolbarRef}
          style={{
            position: 'absolute',
            top: 12,
            left: 12,
            right: 12,
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 8,
            zIndex: 5,
            pointerEvents: 'none',
          }}
        >
          {/* 数量紧挨搜索框：它是「这次搜出来多少」的回执，接在搜索框后面才成句。
              操作按钮留在右上——那是下一步动作，跟「读到了什么」不是一回事 */}
          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, minWidth: 0 }}>
            <Space wrap style={CANVAS_TOP_PILL_STYLE}>
              <Input.Search
                placeholder={t('knowledgeGraph.searchPlaceholder')}
                size="small"
                style={{ width: 180 }}
                value={searchKeyword}
                onChange={(e) => setSearchKeyword(e.target.value)}
                onSearch={handleSearch}
                enterButton={<SearchOutlined />}
              />
              {/* 类型筛选原先画在画布上沿（ECharts 的 scroll legend），12 类一多就折成
                  「1/2 ▶」两页翻着找。换成下拉：可多选、能在框里搜，**空着就是不筛**，
                  所以默认全展示时框内也是空的 */}
              {categories.length > 0 && (
                <Select
                  mode="multiple"
                  size="small"
                  allowClear
                  style={{ width: 190 }}
                  value={pickedTypes}
                  onChange={handleTypeFilterChange}
                  optionFilterProp="value"
                  // 定死 1 个：交给 responsive 去算的话，两次选中就整排标签都塌成「+2」，
                  // 等于看不见筛了什么。留一个名字，其余进 tooltip
                  maxTagCount={1}
                  maxTagPlaceholder={(omitted) => (
                    <Tooltip title={pickedTypes.join('、')}>{`+${omitted.length}`}</Tooltip>
                  )}
                  placeholder={t('knowledgeGraph.entityTypeFilter')}
                  options={categories.map((c) => ({
                    value: c.name,
                    // 圆点沿用图上的配色：颜色与类型的对应关系原本由图例承担，得跟着搬过来
                    label: (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <span
                          style={{
                            width: 8,
                            height: 8,
                            borderRadius: '50%',
                            background: c.itemStyle.color,
                            flexShrink: 0,
                          }}
                        />
                        {c.name}
                      </span>
                    ),
                  }))}
                />
              )}
            </Space>
            {meta && (
              // 11px 是挤出来的：12px 时「搜索框 + 数量」比画布窄后的工具条宽 10px，
              // 数量就得掉到下一行，而它贴回搜索框后面是验收要求。收窄搜索框会让
              // placeholder 被切成「Search entitie…」，那更亏，所以压这颗次要文字
              <Space size={12} wrap style={{ ...CANVAS_TOP_PILL_STYLE, fontSize: 11, color: '#94a3b8' }}>
                <span>
                  {t('knowledgeGraph.showingEntities', { returned: meta.returned_nodes, total: meta.total_nodes })}
                </span>
                <span>
                  {t('knowledgeGraph.showingRelations', { returned: meta.returned_edges, total: meta.total_relations })}
                </span>
                {meta.truncated && (
                  <Tooltip title={t('knowledgeGraph.truncatedTooltip', { limit: NODE_LIMIT })}>
                    <Tag color="orange" style={{ margin: 0, cursor: 'help' }}>
                      {t('knowledgeGraph.truncatedDisplay')}
                    </Tag>
                  </Tooltip>
                )}
                {expanding && <Spin size="small" />}
              </Space>
            )}
          </div>
          {/* marginLeft auto 是为了折行后仍贴右边——单独占一行时 space-between 会把它甩到左侧 */}
          <Space style={{ ...CANVAS_TOP_PILL_STYLE, marginLeft: 'auto' }}>
            <Button size="small" icon={<ZoomInOutlined />} style={{ borderRadius: 8 }} onClick={handleZoomIn} />
            <Button size="small" icon={<ZoomOutOutlined />} style={{ borderRadius: 8 }} onClick={handleZoomOut} />
            <Button size="small" icon={<ExpandAltOutlined />} style={{ borderRadius: 8 }} onClick={handleFitView} />
            <Button size="small" icon={<ReloadOutlined />} style={{ borderRadius: 8 }} onClick={loadData} />
          </Space>
        </div>

        <div
          ref={containerRef}
          style={{
            position: 'absolute',
            top: toolbarOffset,
            left: 0,
            right: 0,
            bottom: CANVAS_STATUS_OFFSET,
          }}
        />

        {/* 操作提示留在画布底部：扣掉高度是它唯一的作用，浮在画布下沿比另占一行更贴题 */}
        {meta && (
          <div
            style={{
              position: 'absolute',
              bottom: 12,
              left: 12,
              right: 12,
              display: 'flex',
              alignItems: 'center',
              // 数量搬去右上后这条只剩一项，靠右与上面的按钮列对齐成一条竖线
              justifyContent: 'flex-end',
              flexWrap: 'wrap',
              gap: 8,
              zIndex: 5,
              pointerEvents: 'none',
            }}
          >
            <Space style={{ ...CANVAS_OVERLAY_STYLE, fontSize: 12, color: '#94a3b8' }}>
              <NodeExpandOutlined />
              <span>{t('knowledgeGraph.dblClickHint')}</span>
            </Space>
          </div>
        )}
        {/* loading 遮罩交给 ECharts 内建（见上面的 useEffect），
            下面三态没有图可展示，仍用手写的 Empty 遮罩 */}
        {error && !loading && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={error}>
              <Button type="primary" onClick={loadData}>
                {t('common.retry')}
              </Button>
            </Empty>
          </div>
        )}
        {!loading && !error && degraded && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={degradedReason || t('knowledgeGraph.degradedReason')}>
              <Button type="primary" onClick={loadData}>
                {t('common.retry')}
              </Button>
            </Empty>
          </div>
        )}
        {!loading && !error && !degraded && currentDataRef.current?.nodes.length === 0 && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={t('knowledgeGraph.noGraphData')} />
          </div>
        )}
      </div>

      {/* 节点详情面板：画布右侧一列，宽度固定，不随内容伸缩。
          外层自己不画东西，只负责「跟画布一样高」——它被 align-items: stretch 拉满行高，
          而真正的内容是绝对定位的，不参与高度计算，所以内容再长也顶不高行、画布不会跟着变形。
          落成 maxHeight 是不行的：行高一旦超过 CANVAS_MIN_HEIGHT（外层容器更高时就会），
          面板就够不着画布的底边，两块对不齐 */}
      {selectedNode && (
        <div style={{ width: 320, flexShrink: 0, position: 'relative' }}>
          <div
            style={{
              position: 'absolute',
              inset: 0,
              overflowY: 'auto',
              background: '#fff',
              borderRadius: 10,
              border: '1px solid #eef2f6',
              boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
              padding: 20,
            }}
          >
            {/* 三样东西挤一行在 320 里放不下，拆成「谁 + 关掉」和「类型 + 动作」两行 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
              <div
                style={{
                  width: 32,
                  height: 32,
                  flexShrink: 0,
                  borderRadius: 8,
                  background: selectedNode.typeColor || '#8b5cf6',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 15,
                  color: '#fff',
                }}
              >
                {selectedNode.name?.charAt(0) || '?'}
              </div>
              <span
                title={selectedNode.name}
                style={{
                  fontSize: 15,
                  fontWeight: 600,
                  color: '#0b2b5c',
                  flex: 1,
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {selectedNode.name}
              </span>
              <Button size="small" type="text" onClick={() => setSelectedNode(null)}>
                ✕
              </Button>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
              <Tag color={selectedNode.typeColor} style={{ margin: 0 }}>
                {selectedNode.type}
              </Tag>
              <Button
                size="small"
                type="primary"
                ghost
                icon={<NodeExpandOutlined />}
                loading={expanding}
                style={{ marginLeft: 'auto' }}
                onClick={() => expandNode(selectedNode)}
              >
                {t('knowledgeGraph.expandNeighbors')}
              </Button>
            </div>
            {/* 两列而非四列：宽度只有原来的三分之一，四列会把值挤成竖排。
                别名长短不一，占满一行免得把旁边那格顶歪 */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}>
              <div>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.entityType')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>{selectedNode.type}</div>
              </div>
              <div>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeGraph.relationCount')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                  {selectedNode.connectedEdges?.length || 0}
                </div>
              </div>
              <div style={{ gridColumn: 'span 2' }}>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.alias')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                  {(() => {
                    const aliases = Array.isArray(selectedNode.aliases)
                      ? selectedNode.aliases
                      : typeof selectedNode.aliases === 'string'
                        ? [selectedNode.aliases]
                        : [];
                    return aliases.length > 0 ? aliases.join(', ') : '-';
                  })()}
                </div>
              </div>
              <div>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('compile.confidence')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                  {selectedNode.confidence != null ? `${(selectedNode.confidence * 100).toFixed(0)}%` : '-'}
                </div>
              </div>
              <div>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.attributeCount')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                  {selectedNode.attributes ? Object.keys(selectedNode.attributes).length : 0}
                </div>
              </div>
              <div>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.sourceDocuments')}</span>
                <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                  {(selectedNode.doc_ids || []).length}
                </div>
              </div>
            </div>
            {selectedNode.description && (
              <div style={{ marginTop: 8 }}>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.description')}</span>
                <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>{selectedNode.description}</div>
              </div>
            )}
            {selectedNode.connectedEdges?.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.relatedRelations')}</span>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
                  {selectedNode.connectedEdges.map((e: any, i: number) => (
                    <Tag key={i} style={{ fontSize: 11 }}>
                      {`${e.source_type}:${e.source}` === selectedNode.id ? '→' : '←'} {e.label}{' '}
                      {`${e.source_type}:${e.source}` === selectedNode.id ? e.target : e.source}
                    </Tag>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
