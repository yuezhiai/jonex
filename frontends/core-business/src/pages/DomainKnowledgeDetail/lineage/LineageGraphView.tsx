/**
 * 血缘画布（P2a，**只读**多跳）：以一张物理表为锚点，按方向/跳数展开
 * `GET /ontology/data-assets` 的子图，交给 `buildLineageGraph` 转成 React Flow
 * 模型后渲染。写入口仍只在列表视图（`LineageTab` 的弹窗，受 `canWrite` 门控）。
 *
 * 语义硬约束（与后端一致）：
 * - 方向：`downstream` 沿 from→to、`upstream` 沿 to→from、`both` 并集；`hops` 1..3；
 * - **`unknown` 显式**：锚点不在投影里 ⇒ 明确文案（「查不到 ≠ 没有上下游」），
 *   不许渲染成「无上下游」；
 * - **`truncated` 显式**：节点/边触顶（200/400）⇒ 顶部 Alert 告知图已截断；
 * - **表在但没有边** ⇒ 仍画锚点单节点，并给「该表暂无上下游血缘」提示，
 *   不是空画布（空画布会被读成「加载失败/什么都没有」）。
 *
 * 锚点候选 = **全图节点表 ∪ 父级血缘边表**（`tables` 只是候选来源之一，不是全部）：
 * 挂载 / 切源时用不带 `table` 的 `getDataAssets` 取整源投影，`nodes[].table` 与
 * `tables` prop 去重排序。join-only 源没有手工血缘边，但投影里有编译 schema 的表
 * —— 候选必须包含全图节点，否则画布会误报「先有血缘边」而打不开。全图加载不阻塞
 * 已有交互；全图请求失败时，**有** `tables` prop 则静默回退（不弹错误 UI，画布照常），
 * **没有**任何候选则显式报错 + 重试（绝不可伪装成「该源还没有表」的假空态）；
 * 全图 `truncated` 在锚点旁给截断提示。
 * **节点点击 = 换锚点**：点到的表只要仍在候选里就不被重置；候选可能不完整
 * （全图请求失败 / `truncated`），此时点到的锚点只要仍在当前画布节点里（或本就
 * 是节点点击选中的）也保留 —— 父级 reload 不得把用户选的锚点重置回 props[0]。
 * 候选与 props 都为空时才显示显式空态（文案说明可重建数据资产或添加血缘边）。
 *
 * 四态齐：loading（Spin）/ error（Alert + 重试，后端 message 原样透出；含全图
 * 列表加载失败）/ empty（全图**成功返回空**且血缘边表也为空、unknown）/ success（画布）。
 *
 * P2b 路径高亮：控件区可再选一张「目标表」（全图表，锚点除外；全图不可用时
 * 回退到当前画布节点 —— 否则 join-only 源的锚点子图只有锚点、目标候选项为空，
 * join 路径无法从 UI 触发），点
 * 「高亮路径」调用 `getLineagePaths`（`max_hops` = 画布 hops、`max_paths` = 5），
 * 由 `applyPathHighlights` 把路径映射到**当前 `buildLineageGraph` 产出的原始边**
 * 上（每次重算都以原始边为输入，不在已高亮的边上二次叠加）；清单面板列出
 * score/transfers/hits_target/steps 与 explored/truncated/unknown/无路径文案。
 * 换锚点/方向/跳数或任何一次重查都会清掉高亮（旧路径结论不再适用）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, Segmented, Select, Space, Spin } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type NodeMouseHandler,
  type ReactFlowInstance,
} from '@xyflow/react';
import { useTranslation } from 'react-i18next';

import { getDataAssets } from '@/api/ontologyDataAssets';
import {
  getLineagePaths,
  type LineagePathsResult,
} from '@/api/ontologyLineagePaths';
import {
  LINEAGE_ORIGIN_COLORS,
  buildLineageGraph,
  lineageEdgeId,
  type LineageEdgeData,
  type LineageGraphModel,
  type LineageRFEdge,
  type LineageRFNode,
} from './lineageGraphModel';
import {
  applyPathHighlights,
  type PathHighlightModel,
} from './lineagePathModel';
import LineagePathPanel from './LineagePathPanel';
import { LineageTableNode } from './LineageTableNode';
// React Flow 基础布局样式（`position: absolute` 的 viewport 等）——不依赖
// `features/ontologyCanvas/styles.scss` 是否已在当前页面加载；只引入库自带 css，
// 不改任何全局样式。
import '@xyflow/react/dist/style.css';

/** 数据流方向（与后端 `direction` 参数同字面量）。 */
type Direction = 'upstream' | 'downstream' | 'both';

/** 画布高度：固定值给 React Flow 一个确定的尺寸（否则 fitView 无参照）。 */
const CANVAS_HEIGHT = 520;

/** 图例：origin 六档 + 对应色（色值以图模型为唯一出处）。 */
const LEGEND_ORIGINS: ReadonlyArray<{ origin: string; labelKey: string }> = [
  { origin: 'declared', labelKey: 'domainKnowledge.lineage.originDeclared' },
  { origin: 'manual', labelKey: 'domainKnowledge.lineage.originManual' },
  { origin: 'comment', labelKey: 'domainKnowledge.lineage.originComment' },
  { origin: 'platform', labelKey: 'domainKnowledge.lineage.originPlatform' },
  { origin: 'value', labelKey: 'domainKnowledge.lineage.originValue' },
  { origin: 'derived', labelKey: 'domainKnowledge.lineage.originDerived' },
];

/** 自定义节点注册表：必须定义在组件外（否则每次渲染都是新对象，React Flow 会整图重挂）。 */
const nodeTypes = { lineageTable: LineageTableNode };

const HOP_OPTIONS = [1, 2, 3] as const;

/** 路径查询固定返回条数（后端 `max_paths` 允许 1..10；画布一次展示 5 条足够）。 */
const MAX_PATHS = 5;

/**
 * 一次成功的高亮结果。`model.edges` **恒由当前 `graph.edges`（原始图边）算出**，
 * 绝不以「上一次高亮后的边」为输入 —— 否则清除/二次高亮会在已着色的边上叠加。
 */
interface PathHighlightState {
  result: LineagePathsResult;
  model: PathHighlightModel<LineageEdgeData>;
  missedPathIndexes: number[];
}

interface Props {
  kbId: string;
  /** 当前页签选中的数据源 id（与列表视图共用） */
  dsId: string;
  /**
   * 锚点候选来源之一：当前源已取血缘边里出现过的表名（`anchorTables(edges)`）。
   * 候选会与全图节点表合并，见组件文档。
   */
  tables: string[];
  /**
   * 边集修订号（父级 `LineageTab` 计算）：同表对新增/删除列级边时 `tables`
   * 不变，必须靠它触发重查（Task 4 验收场景）。
   */
  revision?: string;
}

export default function LineageGraphView({ kbId, dsId, tables, revision }: Props) {
  const { t } = useTranslation();

  /** 锚点表；初值取第一个 props 候选（全图候选返回后会并入并可能补上默认值）。 */
  const [anchor, setAnchor] = useState<string | undefined>(tables[0]);
  const [direction, setDirection] = useState<Direction>('both');
  const [hops, setHops] = useState<number>(1);
  /** 刷新计数：与其它查询参数一样触发 effect。 */
  const [reloadTick, setReloadTick] = useState(0);

  // 初始就是 loading：挂载后必有首个请求（锚点初值 = 第一个候选），
  // 若先按 false 渲染会闪一帧空白画布。
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [graph, setGraph] = useState<LineageGraphModel>();
  /**
   * React Flow 实例：`fitView` 只在首次挂载时生效（同名 prop），换锚点/换方向后
   * 新子图可能落在视口外。每次拿到新图都重新 fit 一次 —— 这是纯视图行为，
   * 不影响数据/请求。
   */
  const flowRef = useRef<ReactFlowInstance<LineageRFNode, LineageRFEdge>>();

  // ── 全图锚点候选（join-only 源也能开画布）──────────────────
  /**
   * 全图节点表名（`getDataAssets` 不传 `table` 的整源快照）。
   * `undefined` = 请求还在途；失败回退成 `[]`（候选只认 `tables` prop）。
   */
  const [fullGraphTables, setFullGraphTables] = useState<string[]>();
  /** 全图节点触顶 ⇒ 锚点候选不完整，控件区给提示。 */
  const [fullGraphTruncated, setFullGraphTruncated] = useState(false);
  const [fullGraphLoading, setFullGraphLoading] = useState(true);
  /**
   * 全图请求失败的原因：`undefined` = 未失败；`''` = 失败但没有后端 message。
   * 只有「一个候选都没有」时才渲染错误态 —— 有 `tables` prop 时仍是静默回退；
   * 后端 message 原样透出，空则渲染通用文案。重试由 `fullGraphTick` 触发重取。
   */
  const [fullGraphError, setFullGraphError] = useState<string>();
  const [fullGraphTick, setFullGraphTick] = useState(0);
  const fullGraphReqRef = useRef(0);
  /**
   * 用户通过「点画布节点」设置的锚点。全图请求失败 / 截断时候选可能不完整，
   * 但它是用户明确的选择 —— 父级 reload（`tables`/`revision` 变化）不得重置。
   * 切源时父级 `key` 重挂，ref 随之归零。
   */
  const nodeClickAnchorRef = useRef<string>();

  // ── 路径高亮（P2b）：目标表 + 请求四态 ──────────────────────
  /** 目标表；全图表（锚点除外），全图不可用时回退到当前画布节点。 */
  const [target, setTarget] = useState<string>();
  const [pathsLoading, setPathsLoading] = useState(false);
  const [pathsError, setPathsError] = useState('');
  /** 有值 ⇒ 面板可见、画布边用高亮版本；undefined ⇒ 面板隐藏、边恢复原始样式。 */
  const [highlight, setHighlight] = useState<PathHighlightState>();

  /**
   * 拉整源投影（不带 `table`）：锚点候选不能只靠血缘边 —— join-only 源一条边
   * 都没有，但投影里有全部编译 schema 表。加载中不阻塞已有画布（props 非空时
   * 候选先用 `tables`）；失败时若还有 `tables` 就静默回退（锚点子图自己的错误
   * 已有 Alert，避免双报）。**一个候选都没有时不能报「没有表」** —— 那是把请求
   * 失败伪装成数据事实；此时记下 `fullGraphError`，由渲染层给错误态 + 重试。
   */
  useEffect(() => {
    const req = ++fullGraphReqRef.current;
    setFullGraphLoading(true);
    setFullGraphTables(undefined);
    setFullGraphTruncated(false);
    setFullGraphError(undefined);
    getDataAssets(kbId, dsId, { direction: 'both', hops: 1 })
      .then((data) => {
        if (fullGraphReqRef.current !== req) return;
        setFullGraphTables(
          (data.nodes ?? [])
            .map((node) => node.table)
            .filter((table) => Boolean(table)),
        );
        setFullGraphTruncated(data.truncated === true);
      })
      .catch((e: any) => {
        if (fullGraphReqRef.current !== req) return;
        // 回退到父级血缘边里的表；画布照常可用。
        setFullGraphTables([]);
        setFullGraphTruncated(false);
        // 后端 message 原样保留（渲染层仅在无任何候选时展示）；没有 message
        // 时记 `''`，渲染层回退到通用文案，避免把失败画成空态。
        setFullGraphError(e?.message || '');
      })
      .finally(() => {
        if (fullGraphReqRef.current === req) setFullGraphLoading(false);
      });
  }, [kbId, dsId, fullGraphTick]);

  /** 血缘边表清单的内容键：父级重渲染但内容不变时不触发重查。 */
  const tablesKey = tables.join('\u0000');

  /**
   * 锚点候选 = 全图节点表 ∪ 血缘边表（去重 + 码点排序）。
   * 全图还没返回时先用 `tables` 顶上，返回后自动并集。
   */
  const anchorOptions = useMemo(() => {
    const merged = new Set<string>();
    for (const table of fullGraphTables ?? []) {
      if (table) merged.add(table);
    }
    for (const table of tables) {
      if (table) merged.add(table);
    }
    return [...merged].sort();
  }, [fullGraphTables, tables]);
  const anchorOptionsKey = anchorOptions.join('\u0000');

  /** 当前画布节点表名集合：候选不完整时用它判断「锚点仍在画布里」。 */
  const graphNodeIds = useMemo(
    () => new Set((graph?.nodes ?? []).map((node) => node.id)),
    [graph],
  );

  /**
   * 候选表变化（全图返回、切源后父级 `key` 重挂、同源内新增/删除边）：
   * - 一个候选都没有 ⇒ 清掉锚点；
   * - 当前锚点仍在候选里 ⇒ **保留**（节点点击可能选到 props 之外、但全图里有的表）；
   * - 当前锚点不在候选里，但**仍在当前画布节点里**，或它本就是用户点节点选的
   *   ⇒ **保留**：全图失败 / `truncated` 时候选不完整，不等于锚点失效；
   * - 否则 ⇒ 优先回落到第一个仍在候选里的 props 表，再退回排序后的 `options[0]`。
   *
   * 依赖用内容键而非数组引用：父级普通重渲染产生的等价数组不会重置锚点。
   */
  useEffect(() => {
    setAnchor((prev) => {
      if (anchorOptions.length === 0) return undefined;
      if (prev && anchorOptions.includes(prev)) return prev;
      // 候选不完整（全图请求失败 / 截断）时，用户点节点选的锚点必须保留 ——
      // 否则下一次父级 reload 会把它静默重置回 props[0]，画布跳回另一张表。
      if (prev && (nodeClickAnchorRef.current === prev || graphNodeIds.has(prev))) {
        return prev;
      }
      const firstProp = tables.find((table) => anchorOptions.includes(table));
      return firstProp ?? anchorOptions[0];
    });
    // 依赖用内容键；`graph` 只在「候选里找不到锚点」时作佐证，不单独驱动本 effect。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchorOptionsKey, tablesKey]);

  /** 请求序号：快速换锚点/方向时丢掉迟到的旧响应。 */
  const reqRef = useRef(0);
  /** 路径查询序号：画布重查 / 清除 / 换目标都会作废在途的路径响应。 */
  const pathsReqRef = useRef(0);

  /**
   * 作废并收起路径高亮（迟到的响应按 `pathsReqRef` 丢弃）：
   * - 画布参数（锚点/方向/跳数）变化或任何一次重查 ⇒ 旧的路径结论已不适用；
   * - 「清除」按钮 ⇒ 面板隐藏、边恢复原始样式；
   * - 目标表变化 ⇒ 清单不再对应当前目标，一并清掉。
   */
  const resetPathHighlight = useCallback(() => {
    pathsReqRef.current += 1;
    setPathsLoading(false);
    setPathsError('');
    setHighlight(undefined);
  }, []);

  const load = useCallback(() => {
    const req = ++reqRef.current;
    // 图一变，旧的路径高亮立即失效（边引用、锚点、跳数都可能已不同）。
    resetPathHighlight();
    if (!anchor) {
      setGraph(undefined);
      setError('');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    getDataAssets(kbId, dsId, { table: anchor, direction, hops })
      .then((data) => {
        if (reqRef.current !== req) return;
        setGraph(buildLineageGraph(data, anchor));
      })
      .catch((e: any) => {
        if (reqRef.current !== req) return;
        // 后端 message 原样透出（如参数被网关 422 拒绝的原因），不吞成通用文案。
        setError(e?.message || t('domainKnowledge.lineage.graph.error'));
        setGraph(undefined);
      })
      .finally(() => {
        if (reqRef.current === req) setLoading(false);
      });
  }, [kbId, dsId, anchor, direction, hops, t, resetPathHighlight]);

  /**
   * 目标表候选 = 全图表（`anchorOptions`，与锚点下拉同源）中除锚点外的表；
   * 全图不可用（在途 / 失败 / 空）时回退到当前画布节点（旧行为）。
   *
   * 不能只取画布节点：join-only 源没有手工血缘边，锚点子图往往只有锚点
   * 单节点 ⇒ 候选为空，后端 `lineage-paths` 支持的 join 路径就无法从 UI
   * 触发。全图里的其他表即使没画在画布上也可以作为目标 —— 路径步骤不在
   * 画布边里时，清单面板会显式标注「不在当前画布」。
   *
   * 顺序：画布节点沿用布局顺序（深度、表名），全图额外表按码点序追加，
   * 下拉不随渲染跳动；全图截断时画布节点也不会因全图不完整而丢失。
   */
  const targetOptions = useMemo(() => {
    if (!graph || graph.unknown) return [];
    const canvasTargets = graph.nodes
      .map((node) => node.id)
      .filter((table) => table !== anchor);
    // 全图不可用（请求在途 / 失败 / 返回空）⇒ 保留旧行为：只列当前画布节点。
    if (!fullGraphTables || fullGraphTables.length === 0) return canvasTargets;
    const seen = new Set(canvasTargets);
    const extras = anchorOptions.filter(
      (table) => table !== anchor && !seen.has(table),
    );
    return [...canvasTargets, ...extras];
  }, [graph, anchor, fullGraphTables, anchorOptions]);

  /** 重查/换锚点后目标表可能已不在候选里：失效即清空，不留悬空选择。 */
  useEffect(() => {
    setTarget((prev) => (prev && targetOptions.includes(prev) ? prev : undefined));
  }, [targetOptions]);

  /**
   * 高亮路径：请求评分路径并应用到**原始图边**（`graph.edges`）上。
   * 每次都以 `graph.edges` 为输入 —— 上一次高亮只存在于 `highlight.model`，
   * 从不回流成下一次的输入（清除/重复高亮不会叠加或残留颜色）。
   */
  const onHighlight = useCallback(() => {
    if (!anchor || !target || !graph) return;
    const req = ++pathsReqRef.current;
    setPathsLoading(true);
    setPathsError('');
    setHighlight(undefined);
    getLineagePaths(kbId, dsId, {
      anchor,
      target,
      maxHops: hops,
      maxPaths: MAX_PATHS,
    })
      .then((result) => {
        if (pathsReqRef.current !== req) return;
        const model = applyPathHighlights(graph.edges, result.paths);
        // 路径边池含 join 边，而画布只有 data-assets 投影里的血缘边：
        // 某些 step 找不到对应画布边是常态，面板需显式说明（清单仍完整列出）。
        const canvasEdgeIds = new Set(graph.edges.map((edge) => edge.id));
        const missedPathIndexes = model.pathSummaries
          .filter((summary) =>
            summary.steps.some(
              (step) => !canvasEdgeIds.has(lineageEdgeId(step.fromTable, step.toTable)),
            ),
          )
          .map((summary) => summary.index);
        setHighlight({ result, model, missedPathIndexes });
      })
      .catch((e: any) => {
        if (pathsReqRef.current !== req) return;
        // 与画布同口径：后端 message 原样透出，不吞成通用文案。
        setPathsError(e?.message || t('domainKnowledge.lineage.paths.error'));
      })
      .finally(() => {
        if (pathsReqRef.current === req) setPathsLoading(false);
      });
  }, [anchor, target, graph, kbId, dsId, hops, t]);

  /** 换目标表 ⇒ 旧清单/高亮立即作废（选空也一样）。 */
  const onTargetChange = useCallback(
    (value?: string) => {
      setTarget(value);
      resetPathHighlight();
    },
    [resetPathHighlight],
  );

  /**
   * 边集内容签名：父级新增/删除血缘边后会 reload 边列表（`tables` 随即变化）。
   * 投影可能已随写入更新，画布必须跟着重查 —— 否则「保存后画布不动」。
   * 用内容而非数组引用作依赖，避免父级普通重渲染带来的无谓请求。
   *
   * ⚠️ 只看 `tables` 不够：**同一表对新增第二条列级边**时表集不变，`columnCount`
   * 会停在旧值。`revision` 由父级按边数/时间戳/表集（+ 每次 reload 序号）算出，
   * 补上「表集相同但边变了」这一档。
   */
  useEffect(() => {
    // `load` 自身已处理「没有锚点 ⇒ 清空画布、不发请求」：候选可能来自全图
    // 而不是 props，所以这里不能再按 `tables.length` 早退。
    load();
  }, [load, reloadTick, tablesKey, revision]);

  // 切锚点/方向/跳数/刷新后是一张新子图：React Flow 的 `fitView` prop 只在
  // 首次挂载生效，新图可能整个落在视口外。每次 `graph` 换对象都重新适配一次。
  useEffect(() => {
    if (!graph || graph.nodes.length === 0) return;
    flowRef.current?.fitView({ padding: 0.2 });
  }, [graph]);

  /** 节点点击 → 设为新锚点（同表不触发重查）；记下「这是用户选的」。 */
  const onNodeClick = useCallback<NodeMouseHandler<LineageRFNode>>((_, node) => {
    nodeClickAnchorRef.current = node.id;
    setAnchor((prev) => (prev === node.id ? prev : node.id));
  }, []);

  // 全图与血缘边都没有可用的表：画布无从谈起 —— 显式空态，不是空画布。
  // 但「全图成功返回空」才算真空态；**请求失败**且无 props 候选时必须报错 +
  // 重试，否则会把网络/后端故障伪装成「该源还没有表」的假数据结论。
  // 全图还在加载时先给加载指示，避免「一闪而过的没有表」误报。
  if (anchorOptions.length === 0) {
    if (fullGraphLoading) {
      return (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin />
          <div style={{ marginTop: 8, color: '#64748b' }}>
            {t('domainKnowledge.lineage.graph.loading')}
          </div>
        </div>
      );
    }
    if (fullGraphError !== undefined) {
      return (
        <Alert
          type="error"
          showIcon
          message={fullGraphError || t('domainKnowledge.lineage.graph.fullGraphError')}
          action={
            <Button
              size="small"
              onClick={() => setFullGraphTick((tick) => tick + 1)}
            >
              {t('common.retry')}
            </Button>
          }
        />
      );
    }
    return <Empty description={t('domainKnowledge.lineage.graph.noTables')} />;
  }

  const unknownAlert = (
    <Alert
      type="warning"
      showIcon
      message={t('domainKnowledge.lineage.graph.unknown', { table: anchor ?? '' })}
    />
  );

  return (
    <div>
      <Space wrap size={[12, 8]} style={{ marginBottom: 12 }}>
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.graph.anchor')}</span>
        <Select
          value={anchor}
          onChange={(value) => setAnchor(value)}
          options={anchorOptions.map((table) => ({ value: table, label: table }))}
          placeholder={t('domainKnowledge.lineage.graph.anchorPlaceholder')}
          style={{ minWidth: 240 }}
          showSearch
          optionFilterProp="label"
        />
        {fullGraphTruncated && (
          <span style={{ color: '#b45309', fontSize: 12 }}>
            {t('domainKnowledge.lineage.graph.anchorTruncated')}
          </span>
        )}
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.graph.direction')}</span>
        <Segmented
          value={direction}
          onChange={(value) => setDirection(value as Direction)}
          options={[
            { value: 'upstream', label: t('domainKnowledge.lineage.graph.upstream') },
            { value: 'downstream', label: t('domainKnowledge.lineage.graph.downstream') },
            { value: 'both', label: t('domainKnowledge.lineage.graph.both') },
          ]}
        />
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.graph.hops')}</span>
        <Select
          value={hops}
          onChange={(value) => setHops(value)}
          options={HOP_OPTIONS.map((count) => ({
            value: count,
            label: t('domainKnowledge.lineage.graph.hopsValue', { count }),
          }))}
          style={{ width: 96 }}
        />
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.paths.target')}</span>
        <Select
          value={target}
          onChange={onTargetChange}
          options={targetOptions.map((table) => ({ value: table, label: table }))}
          placeholder={t('domainKnowledge.lineage.paths.targetPlaceholder')}
          style={{ minWidth: 200 }}
          showSearch
          optionFilterProp="label"
          allowClear
          disabled={targetOptions.length === 0}
        />
        <Button
          type="primary"
          onClick={onHighlight}
          loading={pathsLoading}
          disabled={!target || !graph || loading || pathsLoading}
        >
          {t('domainKnowledge.lineage.paths.highlight')}
        </Button>
        <Button
          onClick={resetPathHighlight}
          disabled={!highlight && !pathsError && !pathsLoading}
        >
          {t('domainKnowledge.lineage.paths.clear')}
        </Button>
        <Button
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => {
            setReloadTick((tick) => tick + 1);
            // 全图候选也重取：否则刚建/删的表要等重挂载才出现在锚点下拉里，
            // 而画布已经刷新过了 —— 两个面看到的「当前表清单」不一致。
            setFullGraphTick((tick) => tick + 1);
          }}
        >
          {t('domainKnowledge.lineage.graph.refresh')}
        </Button>
      </Space>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 12,
          marginBottom: 12,
          fontSize: 12,
          color: '#64748b',
        }}
      >
        <span>{t('domainKnowledge.lineage.graph.legendOrigin')}</span>
        {LEGEND_ORIGINS.map(({ origin, labelKey }) => (
          <span key={origin} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: LINEAGE_ORIGIN_COLORS[origin],
              }}
            />
            {t(labelKey)}
          </span>
        ))}
        <span>{t('domainKnowledge.lineage.graph.edgeLabelHint')}</span>
        <span>{t('domainKnowledge.lineage.graph.edgeWidthHint')}</span>
        <span>{t('domainKnowledge.lineage.graph.hint')}</span>
      </div>

      {loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin />
          <div style={{ marginTop: 8, color: '#64748b' }}>
            {t('domainKnowledge.lineage.graph.loading')}
          </div>
        </div>
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={t('domainKnowledge.lineage.graph.error')}
          description={error}
          action={
            <Button size="small" onClick={() => setReloadTick((tick) => tick + 1)}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : !graph ? null : graph.unknown || graph.nodes.length === 0 ? (
        unknownAlert
      ) : (
        <>
          {graph.truncated && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={t('domainKnowledge.lineage.graph.truncated')}
              description={t('domainKnowledge.lineage.graph.truncatedHint')}
            />
          )}
          {graph.edges.length === 0 && (
            // 表在投影里、只是没有边：锚点单节点照画，另给显式说明。
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={t('domainKnowledge.lineage.graph.noEdges')}
            />
          )}
          <div
            style={{
              height: CANVAS_HEIGHT,
              border: '1px solid #e2e8f0',
              borderRadius: 8,
              overflow: 'hidden',
              background: '#f8fafc',
            }}
          >
            <ReactFlow<LineageRFNode, LineageRFEdge>
              nodes={graph.nodes}
              // 有高亮 ⇒ 画高亮边（由 `graph.edges` 派生）；否则画原始边。
              edges={highlight ? highlight.model.edges : graph.edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.2 }}
              nodesDraggable={false}
              nodesConnectable={false}
              minZoom={0.2}
              maxZoom={2}
              proOptions={{ hideAttribution: true }}
              deleteKeyCode={null}
              onInit={(instance) => {
                flowRef.current = instance;
              }}
              onNodeClick={onNodeClick}
            >
              <Background />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable />
            </ReactFlow>
          </div>

          {/* 路径查询四态：loading / error 就地显示在画布下方；empty·unknown·success 走面板。 */}
          {pathsLoading && (
            <div style={{ padding: 12, textAlign: 'center', color: '#64748b' }}>
              <Spin size="small" />
              <span style={{ marginLeft: 8 }}>
                {t('domainKnowledge.lineage.paths.loading')}
              </span>
            </div>
          )}
          {pathsError && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: 12 }}
              message={t('domainKnowledge.lineage.paths.error')}
              description={pathsError}
              action={
                <Button size="small" onClick={onHighlight}>
                  {t('common.retry')}
                </Button>
              }
            />
          )}
          {highlight && (
            <LineagePathPanel
              result={highlight.result}
              summaries={highlight.model.pathSummaries}
              missedPathIndexes={highlight.missedPathIndexes}
              maxPaths={MAX_PATHS}
            />
          )}
        </>
      )}
    </div>
  );
}
