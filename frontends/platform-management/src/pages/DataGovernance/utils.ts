import type {
  DgAssetDetail,
  DgAssetType,
  DgClassificationCategory,
  DgClassificationDefaultPatch,
  DgClassificationLevel,
  DgClassificationSource,
  DgGraphEdge,
  DgGraphNode,
  DgLineage,
  DgMetadataPatch,
  DgQualityVerdict,
  DgRetentionPolicy,
  DgRetentionPolicyPatch,
  DgRetentionTier,
  DgRetentionWriteScope,
  DgSensitivity,
} from './types';
import {
  DG_DATA_SOURCE_STATUSES,
  DG_DOCUMENT_STATUSES,
  DG_MAX_RENDER_NODES,
  DG_STATUS_COLORS,
  LINEAGE_LAYOUT,
} from './constant';

export function qualityLabelKey(verdict: DgQualityVerdict): string {
  if (verdict === 'ok' || verdict === 'suspect' || verdict === 'bad') {
    return `dataGovernance.quality.${verdict}`;
  }
  return 'dataGovernance.quality.unknown';
}

/**
 * 内置密级文案 key（页面已改用 `levelLabel` 走可配置词表）。
 * 保留此函数仅为兼容旧调用/测试，勿在新代码中使用。
 */
export function sensitivityLabelKey(value: DgSensitivity | null): string {
  return value ? `dataGovernance.sensitivity.${value}` : 'dataGovernance.sensitivity.unclassified';
}

/**
 * 分级级别展示文案（纯函数）。
 *
 * - `code` 为空 → 未分级；
 * - 内置级别且后端名称未被改名 → 复用既有 `sensitivity.*` 文案（保证中英一致）；
 * - 自定义级别或改名后的内置级别 → 用后端返回的名称（管理员自维护，不翻译）；
 * - 配置里查不到该 code → 原样显示 code。
 */
export function levelLabel(
  t: (key: string) => string,
  level: DgClassificationLevel | undefined,
  code: string | null,
): string {
  if (!code) return t('dataGovernance.classification.unclassified');
  const builtinNames: Record<string, string> = {
    public: '公开',
    internal: '内部',
    confidential: '机密',
  };
  if (level && builtinNames[code] && level.name === builtinNames[code]) {
    return t(`dataGovernance.sensitivity.${code}`);
  }
  return level?.name ?? code;
}

/** 分级级别配色：最高 rank 红、`rank >= 2` 金、其余绿；未知/未分级回落 default。 */
export function levelColor(code: string | null, levels: DgClassificationLevel[]): string {
  if (!code) return 'default';
  const rank = levels.find((item) => item.code === code)?.rank;
  if (rank === undefined) return 'default';
  const max = Math.max(...levels.map((item) => item.rank));
  if (rank === max) return 'red';
  if (rank >= 2) return 'gold';
  return 'green';
}

/** 分类字段来源 → i18n key。 */
export function classificationSourceLabelKey(source: DgClassificationSource): string {
  return `dataGovernance.classification.source.${source}`;
}

/**
 * 详情里的资产级覆盖值（来源不是 `asset` 时视为未覆盖）。
 *
 * 表单草稿必须回填**资产级覆盖值**而不是生效值：生效值可能来自租户/KB 默认，
 * 把它写回草稿再保存，会在无意中把继承值固化成资产级覆盖。
 */
export function classificationOverrideDraft(detail: DgAssetDetail): {
  sensitivity: string | null;
  categories: string[] | null;
} {
  return {
    sensitivity:
      detail.classificationSources.sensitivity === 'asset' ? detail.sensitivity : null,
    categories:
      detail.classificationSources.classificationCategories === 'asset'
        ? detail.classificationCategories
        : null,
  };
}

export function statusLabelKey(assetType: DgAssetType, status: string): string {
  const group = assetType === 'document' ? 'document' : 'dataSource';
  // 直接复用 constant.ts 的权威词表（那里已 `as const`），避免两处维护漂移；
  // 放宽到 readonly string[] 才能用任意 string 查表。
  const known: readonly string[] =
    assetType === 'document' ? DG_DOCUMENT_STATUSES : DG_DATA_SOURCE_STATUSES;
  return known.includes(status) ? `dataGovernance.status.${group}.${status}` : status;
}

export function statusColor(status: string): string {
  return DG_STATUS_COLORS[status] ?? 'default';
}

/** 保留策略来源层级 → i18n key（页面用 `t(key)` 展示）。 */
export function retentionSourceLabelKey(source: DgRetentionTier): string {
  return `dataGovernance.retention.source.${source}`;
}

/** i18n key + 参数描述；页面按需传给 `t`。 */
export interface RetentionDaysLabel {
  key: string;
  params?: { count: number };
}

/**
 * 保留天数文案：`0` 表示**不自动到期**（后端合法覆盖，非「未设置」），
 * 其余天数带 `count` 参数。纯函数，不依赖 i18n 实例。
 */
export function retentionDaysLabelKey(days: number): RetentionDaysLabel {
  if (days === 0) return { key: 'dataGovernance.retention.never' };
  return { key: 'dataGovernance.retention.days', params: { count: days } };
}

/**
 * 策略字段的可选值展示：`null/undefined` = 该级未设置（继承），
 * 其余与 `retentionDaysLabelKey` 同口径。返回 `null` 时页面显示「继承」。
 */
export function formatRetentionValue(
  days: number | null | undefined,
): RetentionDaysLabel | null {
  if (days === null || days === undefined) return null;
  return retentionDaysLabelKey(days);
}

/** 保留策略编辑草稿；`null` = 留空（继承上级 / 清除该级覆盖）。 */
export interface RetentionDraft {
  retentionDays: number | null;
  graceDays: number | null;
}

/**
 * 按作用域回填草稿：只有**来源就是该层级**的字段才带值，
 * 其余留空表示继承（两个字段的来源可能不同，必须分别判断）。
 */
export function retentionScopeDraft(
  policy: DgRetentionPolicy | null,
  scope: DgRetentionWriteScope,
): RetentionDraft {
  if (!policy) return { retentionDays: null, graceDays: null };
  return {
    retentionDays: policy.sources.retentionDays === scope ? policy.retentionDays : null,
    graceDays: policy.sources.graceDays === scope ? policy.graceDays : null,
  };
}

/**
 * 构造策略补丁：只发变化字段；草稿里的 `null` 原样发送，表示清除该级覆盖
 * （未出现在补丁里的键保持继承）。空补丁由调用方拦截（后端拒绝空 body）。
 */
export function buildRetentionPatch(
  original: RetentionDraft,
  draft: RetentionDraft,
): DgRetentionPolicyPatch {
  const patch: DgRetentionPolicyPatch = {};
  if (draft.retentionDays !== original.retentionDays) {
    patch.retention_days = draft.retentionDays;
  }
  if (draft.graceDays !== original.graceDays) {
    patch.grace_days = draft.graceDays;
  }
  return patch;
}

type RetentionTranslator = (
  key: string,
  options?: { count: number },
) => string;

/**
 * 策略值展示（抽屉/弹窗共用）：`null` 显示「继承」，`0` 显示「不自动到期」，
 * 其余按天数渲染。i18n 实例由调用方传入，纯函数便于两处复用。
 */
export function retentionValueText(
  t: RetentionTranslator,
  days: number | null | undefined,
): string {
  const label = formatRetentionValue(days);
  if (!label) return t('dataGovernance.lifecycle.inherit');
  return label.params ? t(label.key, label.params) : t(label.key);
}

/** 质量档位配色：suspect 是真实档位，必须与「未评估」区分。 */
export function qualityColor(verdict: DgQualityVerdict): string {
  if (verdict === 'ok') return 'green';
  if (verdict === 'suspect') return 'gold';
  if (verdict === 'bad') return 'red';
  return 'default';
}

/** 文件大小：B 取整，KB/MB/GB 保留 1 位小数；空值显示占位符。 */
export function formatFileSize(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

/** 元数据编辑草稿：空串与 null 都表示「未设置」。 */
export interface MetadataDraft {
  owner: string | null;
  domain: string | null;
  sensitivity: DgSensitivity | null;
  /**
   * 分类类目草稿。`null` = 继承上级 / 未覆盖，`[]` = 显式「无分类」。
   * 必填以强制所有调用点显式决定该字段的语义（避免漏传后静默不发键）。
   */
  categories: string[] | null;
}

const normalizeText = (value: string | null): string | null => {
  const text = (value ?? '').trim();
  return text || null;
};

/**
 * 构造元数据补丁：只发脏字段，空白文本归一为 `null`（清空）。
 *
 * 后端 `extra="forbid"` 且拒绝空 body，所以没有修改时必须返回 `{}` 由调用方拦截。
 */
const sameCategories = (
  a: string[] | null | undefined,
  b: string[] | null | undefined,
): boolean => {
  const left = a ?? null;
  const right = b ?? null;
  if (left === null || right === null) return left === right;
  return left.length === right.length && left.every((value, index) => value === right[index]);
};

export function buildMetadataPatch(original: MetadataDraft, draft: MetadataDraft): DgMetadataPatch {
  const patch: DgMetadataPatch = {};
  const owner = normalizeText(draft.owner);
  const domain = normalizeText(draft.domain);
  if (owner !== normalizeText(original.owner)) patch.owner = owner;
  if (domain !== normalizeText(original.domain)) patch.domain = domain;
  if (draft.sensitivity !== original.sensitivity) patch.sensitivity = draft.sensitivity;
  // 类型上 `categories` 必填；这里的 `undefined` 分支只是**兼容旧调用方的运行时防御**
  // （缺省时不比较、不发键，避免误清空），不是一种合法草稿语义。
  if (
    draft.categories !== undefined &&
    !sameCategories(draft.categories, original.categories)
  ) {
    patch.categories = draft.categories;
  }
  return patch;
}

// ── 批量分级 ─────────────────────────────────────────────────────────

/** 批量弹窗草稿：两级各自独立选择「保持 / 继承 / 设置」等模式。 */
export interface BatchDraft {
  levelMode: 'keep' | 'inherit' | 'set';
  level: string | null;
  categoriesMode: 'keep' | 'inherit' | 'empty' | 'set';
  categories: string[];
}

/**
 * 批量弹窗的字段完整性校验；返回 i18n key 或 null。
 *
 * `keep` / `inherit`（分类另有 `empty`）都是完整的语义，不需要取值；
 * 只有 `set` 必须给出实际选择。等级错误优先返回，保证用户先补齐更关键的一级。
 */
export function batchDraftError(draft: BatchDraft): string | null {
  if (draft.levelMode === 'set' && !(draft.level ?? '').trim()) {
    return 'dataGovernance.classification.levelRequired';
  }
  if (draft.categoriesMode === 'set' && draft.categories.length === 0) {
    return 'dataGovernance.classification.categoryRequired';
  }
  // 两级都不修改 = 空补丁，后端必然 400；前端提前拦截。
  if (draft.levelMode === 'keep' && draft.categoriesMode === 'keep') {
    return 'dataGovernance.classification.noBatchChange';
  }
  return null;
}

/** 下拉选项（等级/分类共用），停用项由 helper 统一置为 disabled。 */
export interface DgClassificationOption {
  value: string;
  label: string;
  disabled: boolean;
}

interface BuildOptionOptions {
  /**
   * 是否禁用停用项。选择表单默认禁用；筛选场景传 `false`
   * （历史资产可能仍带着已停用的级别/分类，筛选取值必须可选）。
   */
  disableInactive?: boolean;
}

/**
 * 等级下拉选项。内置级别且名称未被改名时走 `sensitivity.*` 文案，
 * 自定义级别用后端名称；四处（列表筛选/批量/配置/抽屉）共用，避免文案漂移。
 */
export function buildLevelOptions(
  t: (key: string) => string,
  levels: DgClassificationLevel[],
  options: BuildOptionOptions = {},
): DgClassificationOption[] {
  const disableInactive = options.disableInactive ?? true;
  return levels.map((level) => ({
    value: level.code,
    label: levelLabel(t, level, level.code),
    disabled: disableInactive && !level.isActive,
  }));
}

/**
 * 分类下拉选项。停用项在「禁用 + 带 (No) 后缀」时明确提示管理员；
 * 筛选场景（`disableInactive: false`）保持可选、不加后缀。
 */
export function buildCategoryOptions(
  categories: DgClassificationCategory[],
  t?: (key: string) => string,
  options: BuildOptionOptions = {},
): DgClassificationOption[] {
  const disableInactive = options.disableInactive ?? true;
  return categories.map((category) => ({
    value: category.code,
    label:
      disableInactive && !category.isActive && t
        ? `${category.name} (${t('common.no')})`
        : category.name,
    disabled: disableInactive && !category.isActive,
  }));
}

/**
 * 默认值保存补丁：只发与加载快照不同的键（无变更返回空对象）。
 *
 * 目的之一：某级别/分类被停用后，用户只改另一个字段时，不会把未修改的
 * 停用值一起提交而触发后端 400。
 */
export function buildClassificationDefaultPatch(
  original: { levelCode: string | null; categories: string[] | null },
  draft: { levelCode: string | null; categories: string[] | null },
): DgClassificationDefaultPatch {
  const patch: DgClassificationDefaultPatch = {};
  if (draft.levelCode !== original.levelCode) patch.level_code = draft.levelCode;
  if (!sameCategories(draft.categories, original.categories)) {
    patch.categories = draft.categories;
  }
  return patch;
}

// ── 词表表单校验（与后端约束对齐） ───────────────────────────────────

/** 等级/分类编码：小写字母开头，仅小写字母/数字/下划线，长度 2-32。 */
export const DG_CLASSIFICATION_CODE_PATTERN = /^[a-z][a-z0-9_]{1,31}$/;

/** 保留编码：读侧用它表示「未分级」，不能作为可配置级别编码。 */
const DG_CLASSIFICATION_RESERVED_CODE = 'unclassified';

/** 编码校验；返回 i18n key 或 null。 */
export function classificationCodeError(code: string | null | undefined): string | null {
  const value = (code ?? '').trim();
  if (value === DG_CLASSIFICATION_RESERVED_CODE) {
    return 'dataGovernance.classification.codeReserved';
  }
  if (!DG_CLASSIFICATION_CODE_PATTERN.test(value)) {
    return 'dataGovernance.classification.codeInvalid';
  }
  return null;
}

/** 名称校验（1..64，去空白后判空）；返回 i18n key 或 null。 */
export function classificationNameError(name: string | null | undefined): string | null {
  const value = (name ?? '').trim();
  if (!value) return 'dataGovernance.classification.nameRequired';
  if (value.length > 64) return 'dataGovernance.classification.nameTooLong';
  return null;
}

/** 等级序校验（1..1000 的整数）；返回 i18n key 或 null。 */
export function classificationRankError(
  rank: number | null | undefined,
): string | null {
  if (
    rank === null ||
    rank === undefined ||
    !Number.isFinite(rank) ||
    !Number.isInteger(rank) ||
    rank < 1 ||
    rank > 1000
  ) {
    return 'dataGovernance.classification.rankInvalid';
  }
  return null;
}

/**
 * 批量分级 payload（纯函数）。
 *
 * `keep` 模式**不出现键**（后端保持原值）；`inherit` 发显式 `null`（清除
 * 资产级覆盖、回落租户/KB 默认）；`empty`/`set` 分别发空数组与所选类目。
 *
 * `set` 模式必须给出非空级别编码 —— 否则会退化成 `sensitivity: null`
 * （后端语义是「清除覆盖」），与用户意图相反，因此直接抛错。
 */
export function buildBatchPayload(
  assets: Array<{ assetType: DgAssetType; assetId: string }>,
  draft: BatchDraft,
): {
  assets: Array<{ asset_type: DgAssetType; asset_id: string }>;
  sensitivity?: string | null;
  categories?: string[] | null;
} {
  const payload: {
    assets: Array<{ asset_type: DgAssetType; asset_id: string }>;
    sensitivity?: string | null;
    categories?: string[] | null;
  } = {
    assets: assets.map((item) => ({ asset_type: item.assetType, asset_id: item.assetId })),
  };
  if (draft.levelMode === 'inherit') payload.sensitivity = null;
  if (draft.levelMode === 'set') {
    const level = draft.level?.trim();
    if (!level) throw new Error('批量设置密级缺少级别编码');
    payload.sensitivity = level;
  }
  if (draft.categoriesMode === 'inherit') payload.categories = null;
  if (draft.categoriesMode === 'empty') payload.categories = [];
  if (draft.categoriesMode === 'set') payload.categories = draft.categories;
  return payload;
}

// ── 接口错误元信息 ───────────────────────────────────────────────────

export interface ApiErrorMeta {
  status?: number;
  bizCode?: number;
  message?: string;
}

/**
 * `createRequest` 的 ApiError 带 `status` / `bizCode`，但类型上是 unknown。
 * 只采纳运行时类型正确的字段，避免把任意对象的同名属性当错误信息。
 */
export function errorMeta(err: unknown): ApiErrorMeta {
  if (!err || typeof err !== 'object') return {};
  const meta = err as Record<string, unknown>;
  return {
    status: typeof meta.status === 'number' ? meta.status : undefined,
    bizCode: typeof meta.bizCode === 'number' ? meta.bizCode : undefined,
    message:
      typeof meta.message === 'string' && meta.message.trim() ? meta.message : undefined,
  };
}

/** 写操作错误的二选一文案：`key` 走 i18n，`message` 是后端原文。 */
export type ActionErrorText = { key: string } | { message: string };

/**
 * 统一写操作错误映射（元数据/策略/归档等共用）：
 * - 403 或业务码 3006 → 权限文案；
 * - 拿到 ApiError 的 message → 原样透出（英文界面不串中文）；
 * - 网络错误/未知形态 → 通用失败文案。
 */
export function actionErrorMessage(
  err: unknown,
  options: { forbiddenKey: string; fallbackKey: string },
): ActionErrorText {
  const meta = errorMeta(err);
  if (meta.status === 403 || meta.bizCode === 3006) return { key: options.forbiddenKey };
  if (meta.status !== undefined && meta.message) return { message: meta.message };
  return { key: options.fallbackKey };
}

// ── 血缘图布局 ───────────────────────────────────────────────────────

/**
 * 节点分列：来源 → 文档 → 下游（实体/引用） → 切片。
 *
 * 未知类型与实体/引用同列，保证后端将来新增节点类型时不会画到画布外。
 */
function columnOf(node: DgGraphNode): number {
  if (node.type === 'data_source') return 0;
  if (node.type === 'document') return 1;
  if (node.type === 'chunk') return 3;
  return 2;
}

export interface LineageLayout {
  visibleNodes: DgGraphNode[];
  positions: Record<string, { x: number; y: number; column: number }>;
  width: number;
  height: number;
  hiddenCount: number;
}

/**
 * 轻量血缘图布局（不做图算法，只按类型分列 + 每列围绕画布中心垂直居中）。
 *
 * - 文档节点优先排在 `visibleNodes` 首位，折叠时中心节点不会先被截掉；
 * - 超过 `DG_MAX_RENDER_NODES` 的节点不渲染，只返回 `hiddenCount` 供 UI 提示；
 * - 画布宽度按**实际用到的最大列**计算，窄图不会留出空白列；
 * - 空图返回空可见节点与 `hiddenCount=0`。
 */
export function layoutLineage(lineage: DgLineage): LineageLayout {
  const nodes = lineage.nodes ?? [];
  const document = nodes.find((n) => n.type === 'document');
  const ordered = document ? [document, ...nodes.filter((n) => n !== document)] : [...nodes];
  const visibleNodes = ordered.slice(0, DG_MAX_RENDER_NODES);
  // 先按列分组（保持节点原顺序），再让每列围绕画布垂直中心居中：
  // 节点少的列（文档/切片通常只有 1 个）会落在最大行数的中间行，
  // 而不是贴着顶部，与实体列的中心对齐。
  const columns = new Map<number, DgGraphNode[]>();
  visibleNodes.forEach((node) => {
    const column = columnOf(node);
    const bucket = columns.get(column);
    if (bucket) bucket.push(node);
    else columns.set(column, [node]);
  });
  const maxRows = Math.max(1, ...Array.from(columns.values(), (bucket) => bucket.length));
  const positions: LineageLayout['positions'] = {};
  let maxColumn = 0;
  columns.forEach((bucket, column) => {
    maxColumn = Math.max(maxColumn, column);
    const startY =
      LINEAGE_LAYOUT.top + ((maxRows - bucket.length) * LINEAGE_LAYOUT.nodeGap) / 2;
    bucket.forEach((node, index) => {
      positions[node.id] = {
        x: LINEAGE_LAYOUT.columnX[column],
        y: startY + index * LINEAGE_LAYOUT.nodeGap,
        column,
      };
    });
  });
  return {
    visibleNodes,
    positions,
    width:
      LINEAGE_LAYOUT.columnX[maxColumn] +
      LINEAGE_LAYOUT.nodeWidth +
      LINEAGE_LAYOUT.padding,
    height: LINEAGE_LAYOUT.top + maxRows * LINEAGE_LAYOUT.nodeGap + 20,
    hiddenCount: Math.max(0, ordered.length - visibleNodes.length),
  };
}

/** 血缘图缩放范围。视口自适应与滚轮/按钮缩放共用此上下限。 */
export const LINEAGE_SCALE_MIN = 0.1;
export const LINEAGE_SCALE_MAX = 2;

/** 拖拽平移时图至少要留在视口内的像素数，避免整张图被拖丢。 */
export const LINEAGE_PAN_MARGIN = 40;

/** 视口自适应的内边距：图四周至少留出 24px，避免描边贴边。 */
const FIT_PADDING = 24;

/** 视图变换：`tx/ty` 是画布相对视口的像素平移，`scale` 是缩放倍数。 */
export interface ViewTransform {
  scale: number;
  tx: number;
  ty: number;
}

/** 把缩放倍数夹取到 `LINEAGE_SCALE_MIN ~ LINEAGE_SCALE_MAX`。 */
export function clampScale(value: number): number {
  return Math.max(LINEAGE_SCALE_MIN, Math.min(LINEAGE_SCALE_MAX, value));
}

/**
 * 以视口内 `(anchorX, anchorY)` 为锚点缩放（纯函数）。
 *
 * 锚点下的**世界坐标保持不动**：先按 `scale` 变化比例缩放锚点到原点的距离，
 * 再反推平移量。已到缩放上下限时比例为 1，`tx/ty` 也不会漂移。
 */
export function zoomAt(
  view: ViewTransform,
  nextScale: number,
  anchorX: number,
  anchorY: number,
): ViewTransform {
  const scale = clampScale(nextScale);
  const ratio = scale / view.scale;
  return {
    scale,
    tx: anchorX - (anchorX - view.tx) * ratio,
    ty: anchorY - (anchorY - view.ty) * ratio,
  };
}

/**
 * 平移边界（纯函数）：保证图在视口内至少保留 `LINEAGE_PAN_MARGIN` px。
 *
 * 渲染后的图占据 `[tx, tx + width * scale]`；若该区间与视口重叠不足 40px，
 * 就把 `tx/ty` 拉回最近的合法值。极端窄视口下区间自相矛盾时退化为居中。
 */
export function clampPan(
  view: ViewTransform,
  layout: { width: number; height: number },
  viewport: { width: number; height: number },
): ViewTransform {
  const clampAxis = (value: number, viewportSize: number, graphSize: number): number => {
    const min = LINEAGE_PAN_MARGIN - graphSize;
    const max = viewportSize - LINEAGE_PAN_MARGIN;
    if (min > max) return (viewportSize - graphSize) / 2;
    return Math.min(max, Math.max(min, value));
  };
  return {
    scale: view.scale,
    tx: clampAxis(view.tx, viewport.width, layout.width * view.scale),
    ty: clampAxis(view.ty, viewport.height, layout.height * view.scale),
  };
}

/**
 * 计算「适配视口」的 transform（纯函数，不依赖 DOM）。
 *
 * - 只缩小不放大：`scale` 上限为 1，小图不会被拉伸；
 * - 图宽高按视口比例取较小缩放，保证整图可见；
 * - `tx/ty` 让缩放后的画布在视口内居中；
 * - 缩放夹取到 `LINEAGE_SCALE_MIN ~ LINEAGE_SCALE_MAX`，避免极端比例下不可用。
 */
export function computeFitView(
  layout: { width: number; height: number },
  viewport: { width: number; height: number },
): { scale: number; tx: number; ty: number } {
  const scale = Math.min(
    1,
    (viewport.width - FIT_PADDING * 2) / Math.max(1, layout.width),
    (viewport.height - FIT_PADDING * 2) / Math.max(1, layout.height),
  );
  const clamped = clampScale(scale);
  return {
    scale: clamped,
    tx: (viewport.width - layout.width * clamped) / 2,
    ty: (viewport.height - layout.height * clamped) / 2,
  };
}

/**
 * 单条边的 SVG path（纯函数，端点不在可见节点里返回 `null`）：
 * - 同列（实体间 relation）：从右侧出、右侧入的贝塞尔弧线；
 * - 相邻列：从源右缘到目标左缘的直线（反向边就近取另一侧边缘）；
 * - 跨多列（如 document → chunk）：从顶部总线绕行的正交折线，
 *   避免穿过中间列的节点。
 */
export function lineageEdgePath(layout: LineageLayout, edge: DgGraphEdge): string | null {
  const source = layout.positions[edge.source];
  const target = layout.positions[edge.target];
  if (!source || !target) return null;

  const { nodeWidth, nodeHeight, busY } = LINEAGE_LAYOUT;
  const sy = source.y + nodeHeight / 2;
  const ty = target.y + nodeHeight / 2;
  const columnDiff = target.column - source.column;

  if (columnDiff === 0) {
    const sx = source.x + nodeWidth;
    const tx = target.x + nodeWidth;
    const bulge = nodeWidth / 4;
    return `M ${sx} ${sy} C ${sx + bulge} ${sy}, ${tx + bulge} ${ty}, ${tx} ${ty}`;
  }

  if (Math.abs(columnDiff) === 1) {
    return columnDiff > 0
      ? `M ${source.x + nodeWidth} ${sy} L ${target.x} ${ty}`
      : `M ${source.x} ${sy} L ${target.x + nodeWidth} ${ty}`;
  }

  // 跨多列：source 顶部 → 总线 → target 顶部，正交折线。
  const sx = source.x + nodeWidth / 2;
  const tx = target.x + nodeWidth / 2;
  return `M ${sx} ${source.y} L ${sx} ${busY} L ${tx} ${busY} L ${tx} ${target.y}`;
}
