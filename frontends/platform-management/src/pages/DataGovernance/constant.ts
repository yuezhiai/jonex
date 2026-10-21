import type { DgAssetType } from './types';

/**
 * 资产目录入口权限码 —— **只控菜单/页面可见性**。
 *
 * 注意 data_governance 后端没有 `_ACTION_PERMISSIONS`，数据准入按 KB/空间可见性
 * 独立判定，不要把这个码当成接口鉴权依据。
 */
export const DG_PERMISSION_READ = 'knowledge:read';

export const DG_ASSET_TYPES: DgAssetType[] = ['document', 'data_source'];

export const DG_DOCUMENT_STATUSES = [
  'pending',
  'parsing',
  'ingesting',
  'ready',
  // 归档是筛选目标（页面只排除 deleted），不属于「处理中」。
  'archived',
  'failed',
  'deleting',
  'deleted',
] as const;

export const DG_DATA_SOURCE_STATUSES = ['active', 'paused', 'error'] as const;

/**
 * 状态 → Ant Design Tag 颜色。与上面的状态词表同处维护，避免 utils 再留一份清单。
 * 未登记的状态（如 `deleted`）由 `statusColor` 回落 `default`。
 */
export const DG_STATUS_COLORS: Record<string, string> = {
  ready: 'green',
  active: 'green',
  failed: 'red',
  error: 'red',
  parsing: 'blue',
  ingesting: 'blue',
  pending: 'blue',
  deleting: 'blue',
  // 归档 ≠ 失败：用金色区分「已移出检索但可恢复」。
  archived: 'gold',
};

export const DG_PAGE_SIZE = 10;
export const DG_MAX_PAGE_SIZE = 100;

/**
 * 分页档位（与常量同源）：默认 10 条/页，中间档 20/50。
 * 去重、过滤超上限档位并升序排列；资产目录与标准目录共用，避免两处派生漂移。
 */
export const DG_PAGE_SIZE_OPTIONS: string[] = Array.from(
  new Set([DG_PAGE_SIZE, 20, 50, DG_MAX_PAGE_SIZE]),
)
  .filter((size) => size <= DG_MAX_PAGE_SIZE)
  .sort((a, b) => a - b)
  .map(String);

/** 表格选择列宽度：横向滚动宽度求和时也计入，避免出现隐藏的横向溢出。 */
export const DG_SELECTION_COLUMN_WIDTH = 32;

/** 血缘图最多渲染节点数（超出提示用户收敛方向/深度）。 */
export const DG_MAX_RENDER_NODES = 60;

/**
 * 血缘图布局与渲染常量（`utils.layoutLineage` / `LineageGraph` 共用，避免漂移）。
 *
 * 四列语义：来源 → 文档 → 下游（实体/引用） → 切片。
 */
export const LINEAGE_LAYOUT = {
  columnX: [40, 320, 620, 920],
  nodeWidth: 200,
  nodeHeight: 34,
  /** 同列相邻节点的垂直间距。 */
  nodeGap: 56,
  /** 首行节点 y。 */
  top: 40,
  /** 跨多列边的顶部正交总线 y（在所有节点之上）。 */
  busY: 20,
  /** 列标题基线 y：位于顶部总线之上，文字不会与总线重叠。 */
  columnLabelY: 8,
  /** 画布右侧留白。 */
  padding: 40,
  /** 标签显示宽度上限：CJK/全角记 2、ASCII 记 1（约 15 个汉字）。 */
  maxLabelUnits: 30,
  /** 后端截断提示的阈值；Tag 文案直接使用。 */
  truncatedEntities: 100,
  truncatedRelations: 100,
  truncatedChunks: 50,
} as const;
