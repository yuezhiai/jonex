/** 数据治理资产目录的对外类型（页面只见 camelCase）。 */

export type DgAssetType = 'document' | 'data_source' | 'table' | 'column' | 'metric';
export type DgAssetTypeFilter = DgAssetType | 'all';
/**
 * 密级编码。第 4 期起分级级别可配置，编码不再局限于内置三种，
 * 因此收敛为 `string`；权威词表由 `listClassificationConfig()` 提供。
 */
export type DgSensitivity = string;
/**
 * 分类字段的来源：资产级覆盖 / 表级（列继承表覆盖） / 知识库级 / 租户级 / 未设置。
 */
export type DgClassificationSource = 'asset' | 'table' | 'kb' | 'tenant' | 'none';
/**
 * 质量档位。
 *
 * `'suspect'` 在引擎侧枚举中存在，但数据治理后端**暂不产出** —— 保留类型诚实，
 * 避免页面把「可疑」误当成未知；质量文案在后续任务补 `suspect` 分支。
 */
export type DgQualityVerdict = 'ok' | 'suspect' | 'bad' | null;
export type DgDirection = 'up' | 'down' | 'both';

/** 保留策略层级，从细到粗：document → kb → tenant → system。 */
export type DgRetentionTier = 'document' | 'kb' | 'tenant' | 'system';

/**
 * 可写作用域：`system` 只是内置默认值来源，**不能作为写入目标**
 * （`setRetentionPolicy('system', ...)` 应在编译期失败）。
 */
export type DgRetentionWriteScope = 'document' | 'kb' | 'tenant';

/**
 * 生效保留策略（详情接口的 `retention_policy` 投影）。
 *
 * - `retentionDays` / `graceDays` 是**字段级继承后的生效值**；
 * - `effectiveFrom` 只描述 `retentionDays` 的生效层级；
 * - `sources` 给出两个字段各自的来源层级（两者可能不同）。
 */
export interface DgRetentionPolicy {
  scope: DgRetentionTier;
  effectiveFrom: DgRetentionTier;
  retentionDays: number;
  graceDays: number;
  sources: {
    retentionDays: DgRetentionTier;
    graceDays: DgRetentionTier;
  };
}

/**
 * 策略写入补丁：显式 `null` = 清除该级覆盖，未出现的键保持继承。
 * 键名沿用后端 snake_case，API 层原样透传。
 */
export interface DgRetentionPolicyPatch {
  retention_days?: number | null;
  grace_days?: number | null;
}

/**
 * 归档 / 恢复 / 销毁的返回：后端只回最小状态，前端可直接更新本地行。
 * - archive：`{id, status:'archived', archived:true}`
 * - restore：`{id, status:<恢复后状态>, restored:true}`
 * - destroy：`{id, deleted:true}`（幂等命中时可能带 `status:'deleted'`）
 */
export interface DgLifecycleResult {
  id: string;
  status?: string;
  archived?: boolean;
  restored?: boolean;
  deleted?: boolean;
}

export interface DgAssetItem {
  assetType: DgAssetType;
  assetId: string;
  name: string;
  kbId: string;
  kbName: string;
  spaceId: string;
  status: string;
  updatedAt: string;
  owner: string | null;
  domain: string | null;
  sensitivity: DgSensitivity | null;
  /** 分类类目编码（空数组 = 未分类）。 */
  classificationCategories: string[];
  classificationSources: DgClassificationSourceMap;
  qualityVerdict: DgQualityVerdict;
  /** 派生资产的上级标识（列 → 表、表 → 数据源、指标 → KB）。 */
  parentId?: string | null;
  /** 表/列所属数据源；指标为所在 KB；文档/数据源本身为 null。 */
  sourceId?: string | null;
  /** 来源展示名：表/列为数据源名，指标为 KB 名。 */
  sourceLabel?: string | null;
  /** per-source / 抽象 schema 的版本号。 */
  schemaVersion?: number | null;
}

export interface DgQuality {
  verdict: DgQualityVerdict;
  reasons: string[];
}

export interface DgAssetDetail extends DgAssetItem {
  fileSize: number | null;
  mimeType: string | null;
  dataSourceType: string | null;
  folderId: string | null;
  contentGeneration: number | null;
  ontologyStatus: string | null;
  accessType: string | null;
  syncMode: string | null;
  lastSyncAt: string | null;
  lastSyncStatus: string | null;
  documentCount: number | null;
  quality: DgQuality;
  /** 创建时间；详情接口返回，缺失时为 null。 */
  createdAt: string | null;
  /** 归档时间；未归档为 null。 */
  archivedAt: string | null;
  /**
   * 生效保留策略：文档按 document → kb → tenant 三级继承；数据源也会返回
   * KB/租户级策略（后端以 `document_id=""` 解析），并非恒为 null。
   * 仅在策略投影失败时才是 null。
   */
  retentionPolicy: DgRetentionPolicy | null;
  // ── W2.1 派生资产（表 / 列 / 指标）专属字段 ──────────────────────
  /** 表/列所属数据源。 */
  dataSourceId?: string | null;
  /** 表名（列资产为其所属表）。 */
  table?: string | null;
  /** 列类型。 */
  columnType?: string | null;
  /** 列是否主键；非列资产为 null。 */
  primaryKey?: boolean | null;
  /** 列显示名。 */
  displayName?: string | null;
  /** 表的列清单（键已转 camelCase；非表资产为空/缺省）。 */
  columns?: {
    name: string;
    type: string | null;
    primaryKey: boolean;
    displayName: string | null;
  }[];
  /** 表的列数（与 `columns` 同源）。 */
  columnCount?: number | null;
  /** 指标描述。 */
  description?: string | null;
  /** 指标可加性。 */
  additivity?: string | null;
  /** 指标别名。 */
  aliases?: string[];
  /** 指标落点（`table` 为空 = 未解析到物理表）。 */
  landings?: {
    dataSourceId: string;
    entityName?: string;
    diMetric?: string;
    table: string | null;
  }[];
  /** 指标没有任何解析到物理表的落点。 */
  unresolved?: boolean;
  /** per-source compiled schema 同步状态。 */
  syncStatus?: string | null;
}

export interface DgAssetListParams {
  assetType?: DgAssetTypeFilter;
  spaceId?: string;
  kbId?: string;
  domain?: string;
  owner?: string;
  sensitivity?: DgSensitivity;
  /** 分类类目筛选（按类目编码精确匹配）。 */
  category?: string;
  status?: string;
  keyword?: string;
  page: number;
  pageSize: number;
}

export interface DgAssetListResult {
  items: DgAssetItem[];
  total: number;
}

/** 两个分类字段各自的来源（两者可能不同，必须分别展示）。 */
export interface DgClassificationSourceMap {
  sensitivity: DgClassificationSource;
  classificationCategories: DgClassificationSource;
}

/** 分级级别配置；`rank` 越大越敏感，用于配色与排序。 */
export interface DgClassificationLevel {
  code: string;
  name: string;
  rank: number;
  isActive: boolean;
}

/** 分类类目配置（如 `pii` / `financial`）。 */
export interface DgClassificationCategory {
  code: string;
  name: string;
  isActive: boolean;
}

export interface DgClassificationConfig {
  levels: DgClassificationLevel[];
  categories: DgClassificationCategory[];
}

/** 租户级 / 知识库级默认分级。`levelCode` / `categories` 为 null 表示未设置。 */
export interface DgClassificationDefault {
  scopeType: 'tenant' | 'kb';
  scopeId: string;
  levelCode: string | null;
  categories: string[] | null;
}

/** 默认值写入补丁：键名沿用后端 snake_case，只发调用方给了的键。 */
export interface DgClassificationDefaultPatch {
  level_code?: string | null;
  categories?: string[] | null;
}

/**
 * 用户密级配置行。
 *
 * `maxLevelCode=null` 表示该用户未单独配置（按默认最低等级可读），
 * 与「配置为空串」不同，页面不能把两者混为一谈。
 */
export interface DgUserClearance {
  userId: string;
  username: string;
  displayName: string;
  maxLevelCode: string | null;
  isActive: boolean;
}

export interface DgMetadataPatch {
  owner?: string | null;
  domain?: string | null;
  sensitivity?: DgSensitivity | null;
  categories?: string[] | null;
}

export interface DgGraphNode {
  id: string;
  type: string;
  label: string;
  meta: Record<string, unknown>;
}

export interface DgGraphEdge {
  source: string;
  target: string;
  type: string;
  meta: Record<string, unknown>;
}

export interface DgLineage {
  asset: { assetType: DgAssetType; assetId: string; name: string };
  nodes: DgGraphNode[];
  edges: DgGraphEdge[];
  counts: {
    chunks: number | null;
    entities: number;
    relations: number;
    citations: number;
    /** 派生资产血缘的节点/边计数（文档图缺省不出现）。 */
    nodes?: number;
    edges?: number;
  };
  truncated: {
    entities: boolean;
    relations: boolean;
    chunks: boolean;
  };
  /**
   * 派生资产血缘的降级标记：`true` = 血缘来源（PG/对象存储）不可用，
   * 节点保留、边为空。文档血缘恒为 `false`。
   */
  degraded?: boolean;
  /** 指标血缘无落点。文档/表/列血缘恒为 `false`。 */
  unresolved?: boolean;
}

// ── W2.2 标准资产（术语 / 数据元 / 指标口径卡） ─────────────────────────

/** 标准资产的三种类型。 */
export type DgStandardAssetKind = 'term' | 'data_element' | 'metric_card';

/** 标准资产行状态：草稿 → 已发布 → 已退役。 */
export type DgStandardAssetStatus = 'draft' | 'published' | 'retired';

/**
 * 标准资产列表行（已由 api 层完成 snake_case → camelCase）。
 *
 * `draftVersion` / `publishedVersion` 缺失为 `null`；`hasDraft` 表示「存在
 * 未发布的草稿」，后端只在管理员带 `include_drafts` 时才会给出真实值。
 */
export interface DgStandardAssetItem {
  assetId: string;
  kind: DgStandardAssetKind;
  code: string;
  name: string;
  version: number;
  status: DgStandardAssetStatus;
  /** 乐观锁版本号，写/发布时必须回传。 */
  revision: number;
  publishedVersion: number | null;
  draftVersion: number | null;
  hasDraft: boolean;
  updatedAt: string | null;
  updatedBy: string | null;
}

/** 详情 = 列表行 + 完整的 JSON payload（可能为空对象）。 */
export interface DgStandardAssetDetail extends DgStandardAssetItem {
  payload: Record<string, unknown>;
}

/** 列表查询参数（camelCase；api 层转 snake_case 后发送）。 */
export interface DgStandardAssetListParams {
  kind?: DgStandardAssetKind | '';
  status?: DgStandardAssetStatus | '';
  keyword?: string;
  /** 仅管理员有效；只有显式为 `true` 时才发送。 */
  includeDrafts?: boolean;
  page: number;
  pageSize: number;
}

export interface DgStandardAssetListResult {
  items: DgStandardAssetItem[];
  total: number;
}

/** 新建草稿请求体。`baseVersion` 给定时按该版本克隆（name/payload 被忽略）。 */
export interface DgStandardAssetDraftBody {
  kind: DgStandardAssetKind;
  code: string;
  name: string;
  payload?: Record<string, unknown>;
  baseVersion?: number;
}

/** 更新草稿请求体。`expectedRevision` 用于乐观锁。 */
export interface DgStandardAssetUpdateBody {
  expectedRevision: number;
  name?: string;
  payload?: Record<string, unknown>;
}

// ── W2.3 数据合同（ODCS v3.x，数据源级、版本化） ──────────────────────

/** 数据合同状态：草稿 → 已发布 → 已退役。 */
export type DgDataContractStatus = 'draft' | 'published' | 'retired';

/**
 * 数据合同列表行（已由 api 层完成 snake_case → camelCase）。
 *
 * `publishedVersion` / `draftVersion` 缺失为 `null`；`hasDraft` 表示「存在未发布
 * 的草稿」，后端只在管理员带 `include_drafts` 时才给出真实值。
 */
export interface DgDataContractItem {
  contractId: string;
  dataSourceId: string;
  dataSourceName: string | null;
  name: string;
  version: number;
  status: DgDataContractStatus;
  /** 乐观锁版本号，写/发布时必须回传。 */
  revision: number;
  publishedVersion: number | null;
  draftVersion: number | null;
  hasDraft: boolean;
  updatedAt: string | null;
  updatedBy: string | null;
}

/** 详情 = 列表行 + 完整的 ODCS payload（可能为空对象）。 */
export interface DgDataContractDetail extends DgDataContractItem {
  payload: Record<string, unknown>;
}

/** 列表查询参数（camelCase；api 层转 snake_case 后发送）。 */
export interface DgDataContractListParams {
  keyword?: string;
  status?: DgDataContractStatus | '';
  /** 仅管理员有效；只有显式为 `true` 时才发送。 */
  includeDrafts?: boolean;
  page: number;
  pageSize: number;
}

export interface DgDataContractListResult {
  items: DgDataContractItem[];
  total: number;
}

/** 新建草稿请求体。`baseVersion` 给定时按该版本克隆（name/payload 由后端覆盖）。 */
export interface DgDataContractDraftBody {
  dataSourceId: string;
  payload?: Record<string, unknown>;
  name?: string;
  baseVersion?: number;
}

/** 更新草稿请求体。`expectedRevision` 用于乐观锁。 */
export interface DgDataContractUpdateBody {
  expectedRevision: number;
  payload?: Record<string, unknown>;
  name?: string;
}

/** 导入 ODCS 合同请求体。`format` 缺省时后端按内容首字符判定。 */
export interface DgDataContractImportBody {
  dataSourceId: string;
  content: string;
  format?: DgContractFormat | '';
}

/** 合同的序列化格式；前端只负责 JSON 的新建/编辑，YAML 由后端导出。 */
export type DgContractFormat = 'json' | 'yaml';

/** 脚手架预览的统计（api 层已完成 snake_case → camelCase）。 */
export interface DgContractScaffoldStats {
  tables: number;
  columns: number;
  skippedTables: number;
  skippedColumns: number;
}

/** 脚手架预览结果：payload 可直接作为新建草稿的初始内容。 */
export interface DgContractScaffoldResult {
  dataSourceId: string;
  /** 生成时所依据的 per-source compiled schema 版本；缺失为 null。 */
  schemaVersion: number | null;
  payload: Record<string, unknown>;
  stats: DgContractScaffoldStats;
  warnings: string[];
}

/** 导出结果：内容与文件名由后端给，前端不做 Blob 转换。 */
export interface DgDataContractExportResult {
  content: string;
  format: DgContractFormat;
  filename: string;
}

/** 可建合同的数据源下拉项：只含 `access_type === 'database'` 的源。 */
export interface DgContractDataSourceOption {
  id: string;
  name: string;
  accessType: string;
}

// ── W2.5 数据质量闭环 ────────────────────────────────────────────────

export type DgQualityDimension =
  | 'completeness' | 'uniqueness' | 'validity' | 'consistency' | 'timeliness';

export type DgQualitySeverity = 'info' | 'warning' | 'error' | 'critical';
export type DgQualityCheckType = 'library' | 'sql' | 'custom';
export type DgQualityRunStatus = 'running' | 'completed' | 'failed';
export type DgQualityResultStatus = 'passed' | 'failed' | 'error';
export type DgQualityIssueStatus =
  | 'open' | 'assigned' | 'fixing' | 'pending_verify' | 'closed';

/** 质量规则（后端行已由 api 层转 camelCase）。 */
export interface DgQualityRule {
  id: string;
  name: string;
  assetType: 'table' | 'column';
  assetId: string;
  dataSourceId: string;
  kbId: string | null;
  dimension: DgQualityDimension;
  severity: DgQualitySeverity;
  checkType: DgQualityCheckType;
  definition: Record<string, unknown>;
  threshold: Record<string, unknown>;
  /** 0 / 1（后端严格整数，不接受布尔）。 */
  enabled: number;
  contractId: string | null;
  revision: number;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface DgQualityRuleListParams {
  dataSourceId?: string;
  assetType?: 'table' | 'column' | '';
  assetId?: string;
  dimension?: DgQualityDimension | '';
  severity?: DgQualitySeverity | '';
  checkType?: DgQualityCheckType | '';
  enabled?: 0 | 1;
  keyword?: string;
  page: number;
  pageSize: number;
}

export interface DgQualityRuleListResult {
  items: DgQualityRule[];
  total: number;
}

/** 新建/编辑共用的规则正文。 */
export interface DgQualityRuleBody {
  name: string;
  assetType: 'table' | 'column';
  assetId: string;
  dimension: DgQualityDimension;
  severity: DgQualitySeverity;
  checkType: DgQualityCheckType;
  definition: Record<string, unknown>;
  threshold: Record<string, unknown>;
  enabled?: 0 | 1;
  kbId?: string | null;
}

export interface DgQualityRuleUpdateBody extends Partial<DgQualityRuleBody> {
  expectedRevision: number;
}

export interface DgQualityRuleImportResult {
  created: number;
  skipped: number;
  warnings: string[];
}

/** 预览/运行规则清单里的一条 SQL。`sql=null` 表示 sql/custom 类型需外部执行。 */
export interface DgQualitySqlItem {
  ruleId: string;
  name: string;
  sql: string | null;
}

export interface DgQualityPreviewResult {
  rules: DgQualitySqlItem[];
  warnings: string[];
}

export interface DgQualityRun {
  id: string;
  dataSourceId: string;
  trigger: string;
  status: DgQualityRunStatus;
  ruleIds: string[];
  executor: string | null;
  dialect: string;
  total: number;
  passed: number;
  failed: number;
  error: number;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string | null;
}

export interface DgQualityRunListParams {
  dataSourceId?: string;
  status?: DgQualityRunStatus | '';
  page: number;
  pageSize: number;
}

export interface DgQualityRunListResult {
  items: DgQualityRun[];
  total: number;
}

export interface DgQualityRunCreateBody {
  dataSourceId: string;
  ruleIds?: string[];
  trigger?: string;
  executor?: string;
  dialect?: string;
}

export interface DgQualityRunCreateResult {
  run: DgQualityRun;
  rules: DgQualitySqlItem[];
  warnings: string[];
}

export interface DgQualityResultItem {
  ruleId: string;
  assetType: 'table' | 'column';
  assetId: string;
  status: DgQualityResultStatus;
  metricValue: unknown;
  message: string | null;
  sample: unknown[] | null;
  checkedAt: string | null;
}

/** 创建运行时的规则快照（规则被删/改后仍按它回传结果与开单）。 */
export interface DgQualityRuleSnapshotItem {
  ruleId: string;
  name: string;
  assetType: 'table' | 'column';
  assetId: string;
  severity: DgQualitySeverity;
  /** `null` = sql/custom 规则，需外部执行器执行。 */
  sql: string | null;
}

export interface DgQualityRunDetail extends DgQualityRun {
  results: DgQualityResultItem[];
  ruleSnapshot: DgQualityRuleSnapshotItem[];
}

export interface DgQualityResultSubmitItem {
  ruleId: string;
  status: DgQualityResultStatus;
  metricValue?: unknown;
  message?: string;
  sample?: unknown[];
}

export interface DgQualityRunSubmitResult {
  run: DgQualityRun;
  counts: { total: number; passed: number; failed: number; error: number };
}

export interface DgQualityIssue {
  id: string;
  ruleId: string;
  assetType: 'table' | 'column';
  assetId: string;
  dataSourceId: string;
  severity: DgQualitySeverity;
  status: DgQualityIssueStatus;
  assigneeId: string | null;
  title: string;
  detail: string | null;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  resolvedAt: string | null;
  revision: number;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface DgQualityIssueEvent {
  id: string;
  issueId: string;
  eventType: string;
  fromStatus: string | null;
  toStatus: string | null;
  actorId: string | null;
  note: string | null;
  createdAt: string | null;
}

export interface DgQualityIssueDetail extends DgQualityIssue {
  events: DgQualityIssueEvent[];
}

export interface DgQualityIssueListParams {
  status?: DgQualityIssueStatus | '';
  severity?: DgQualitySeverity | '';
  assigneeId?: string;
  dataSourceId?: string;
  assetType?: 'table' | 'column' | '';
  assetId?: string;
  keyword?: string;
  page: number;
  pageSize: number;
}

export interface DgQualityIssueListResult {
  items: DgQualityIssue[];
  total: number;
}

export interface DgQualitySummaryRun {
  status: string;
  finishedAt: string | null;
  passed: number;
  failed: number;
  error: number;
}

export interface DgQualitySummaryItem {
  dataSourceId: string;
  name: string | null;
  totalRules: number;
  enabledRules: number;
  latestRun: DgQualitySummaryRun | null;
  openIssues: Record<DgQualitySeverity, number>;
}

export interface DgQualitySummary {
  dataSources: DgQualitySummaryItem[];
  totals: { rules: number; openIssues: number; lastRunAt: string | null };
}

// ── W2.6 资源视图（业务域 / 责任 / 落标 / 原文锚点） ──────────────────
//
// 后端 snake_case 只出现在 `src/api/resourceViews.ts`；以下类型全部是
// camelCase 的页面侧形状。

/** 资源视图资产计数：五类资产固定键（与后端 `ASSET_TYPES` 同口径）。 */
export interface DgResourceAssetCounts {
  table: number;
  column: number;
  metric: number;
  document: number;
  dataSource: number;
}

/** 分级计数五档；`unclassified` 只计空值，词表外自定义级别不计数。 */
export interface DgResourceSensitivity {
  public: number;
  internal: number;
  confidential: number;
  secret: number;
  unclassified: number;
}

/** 未关闭质量问题四档（`quality_issues.severity`）。 */
export interface DgResourceOpenIssues {
  critical: number;
  error: number;
  warning: number;
  info: number;
}

export interface DgResourceOwnerCount {
  owner: string;
  count: number;
}

/** 业务域视图的一个分组：`(space_id, space_name, domain)`。 */
export interface DgDomainViewGroup {
  spaceId: string | null;
  spaceName: string | null;
  /** 空 domain 在后端归一为 `__unassigned__`，展示走 `domainLabel`。 */
  domain: string;
  assetCounts: DgResourceAssetCounts;
  ownerCount: number;
  owners: DgResourceOwnerCount[];
  sensitivity: DgResourceSensitivity;
  openIssues: DgResourceOpenIssues;
}

/** 业务域视图全局合计（与分组同口径，附分组数）。 */
export interface DgDomainViewTotals {
  assetCounts: DgResourceAssetCounts;
  ownerCount: number;
  owners: DgResourceOwnerCount[];
  sensitivity: DgResourceSensitivity;
  openIssues: DgResourceOpenIssues;
  groups: number;
}

export interface DgDomainViewResult {
  groups: DgDomainViewGroup[];
  totals: DgDomainViewTotals;
  /** 全空时显式为 true，不伪造分组。 */
  empty: boolean;
  /** 资产分页达到上限：计数可能不完整。 */
  truncated: boolean;
}

export interface DgDomainViewParams {
  spaceId?: string;
  domain?: string;
}

export type DgResponsibilityGroupBy = 'owner' | 'domain';

/** 责任视图的一行：owner 或 domain 分组。 */
export interface DgResponsibilityRow {
  /** 原始分组键；空 owner/domain 为 `__unowned__` / `__unassigned__`。 */
  key: string;
  /** 展示名：owner 匹配平台用户时为 display_name，其余与 key 相同。 */
  displayName: string;
  assetCounts: DgResourceAssetCounts;
  /** 分组行的 distinct owner 数；domain 视图不查用户表，可能省略。 */
  ownerCount?: number;
  sensitivity: DgResourceSensitivity;
  openIssues: DgResourceOpenIssues;
  lastUpdatedAt: string | null;
}

export interface DgResponsibilityTotals {
  assetCounts: DgResourceAssetCounts;
  ownerCount: number;
  owners: DgResourceOwnerCount[];
  sensitivity: DgResourceSensitivity;
  openIssues: DgResourceOpenIssues;
  groups: number;
  unowned: number;
  unassignedDomain: number;
}

export interface DgResponsibilityResult {
  rows: DgResponsibilityRow[];
  totals: DgResponsibilityTotals;
  groupBy: DgResponsibilityGroupBy;
  empty: boolean;
  /** 资产分页达到上限：计数可能不完整。 */
  truncated: boolean;
}

export interface DgResponsibilityParams {
  groupBy: DgResponsibilityGroupBy;
  spaceId?: string;
}

export type DgBindingStatus = 'active' | 'disabled';
/** 人工创建 / 建议采纳（建议只读候选，确认后落库为 `suggested`）。 */
export type DgBindingSource = 'manual' | 'suggested';
/** 落标可指向的资产类型（与迁移 CHECK 一致）。 */
export type DgBindingAssetType = 'table' | 'column' | 'metric';

export interface DgStandardBinding {
  id: string;
  tenantId: string;
  standardKind: DgStandardAssetKind;
  standardCode: string;
  standardAssetId: string | null;
  assetType: DgBindingAssetType;
  assetId: string;
  source: DgBindingSource;
  confidence: number | null;
  note: string | null;
  status: DgBindingStatus;
  revision: number;
  createdBy: string | null;
  updatedBy: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface DgBindingListParams {
  standardKind?: DgStandardAssetKind | '';
  standardCode?: string;
  assetType?: DgBindingAssetType | '';
  assetId?: string;
  status?: DgBindingStatus | '';
  page: number;
  pageSize: number;
}

export interface DgBindingListResult {
  items: DgStandardBinding[];
  total: number;
  page: number;
  pageSize: number;
}

export interface DgBindingCreateBody {
  standardKind: DgStandardAssetKind;
  standardCode: string;
  assetType: DgBindingAssetType;
  assetId: string;
  note?: string;
  source?: DgBindingSource;
  confidence?: number;
}

export interface DgBindingUpdateBody {
  expectedRevision: number;
  note?: string;
  status?: DgBindingStatus;
}

/** 覆盖率行里已经落地的绑定（后端只给最近 20 条）。 */
export interface DgCoverageBoundAsset {
  bindingId: string;
  assetType: DgBindingAssetType;
  assetId: string;
  source: DgBindingSource;
  confidence: number | null;
}

export interface DgCoverageRow {
  standardAssetId: string;
  standardKind: DgStandardAssetKind;
  standardCode: string;
  name: string;
  version: number;
  domain: string | null;
  status: string;
  bindingsCount: number;
  boundAssets: DgCoverageBoundAsset[];
  /** `bindings_count == 0`；后端显式给值，缺失时按绑定数推导。 */
  unbound: boolean;
}

export interface DgCoverageSummary {
  totalStandards: number;
  boundStandards: number;
  bindingCount: number;
  /** 0..1 比率；分母为 0 时后端显式 null，前端展示 `—`。 */
  coverage: number | null;
}

export interface DgCoverageParams {
  kind?: DgStandardAssetKind | '';
  domain?: string;
  page: number;
  pageSize: number;
}

export interface DgCoverageResult {
  items: DgCoverageRow[];
  total: number;
  page: number;
  pageSize: number;
  summary: DgCoverageSummary;
}

/** 落标建议候选（只读，不落库）。 */
export interface DgBindingSuggestion {
  standardKind: DgStandardAssetKind;
  standardCode: string;
  standardName: string;
  /** 与 `standardName` 同值；后端两处同名字段都返回。 */
  name: string;
  assetType: DgBindingAssetType;
  assetId: string;
  assetName: string;
  confidence: number | null;
  reason: string | null;
}

export interface DgBindingSuggestionParams {
  standardKind?: DgStandardAssetKind | '';
  standardCode?: string;
  assetType?: DgBindingAssetType | '';
  assetId?: string;
  limit?: number;
}

/** 原文锚点的资产标识（锚点口径 = 文档 + 实体名/属性，不含页码）。 */
export interface DgSourceAnchorAsset {
  assetType: DgAssetType;
  assetId: string;
}

export interface DgSourceAnchorDocument {
  id: string;
  name: string;
  status: string;
}

/** 一条 active 对齐命中的文档实体。 */
export interface DgSourceAnchorLink {
  knowledgeBaseId: string;
  docType: string;
  entityName: string;
  entityCount: number;
  /** 实例数超过单次查询上限时为 true。 */
  truncated: boolean;
  attributes: Record<string, unknown>;
  documents: DgSourceAnchorDocument[];
}

export interface DgSourceAnchorResult {
  asset: DgSourceAnchorAsset;
  linked: DgSourceAnchorLink[];
  /** 无对齐/实体/文档：显式提示，不伪装成「没有原文」。 */
  unresolved: boolean;
  /** 上游 KB/Neo4j 失败：fail-open，页面须显式提示数据不可信。 */
  degraded: boolean;
}
