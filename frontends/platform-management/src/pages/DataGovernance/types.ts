/** 数据治理资产目录的对外类型（页面只见 camelCase）。 */

export type DgAssetType = 'document' | 'data_source';
export type DgAssetTypeFilter = DgAssetType | 'all';
/**
 * 密级编码。第 4 期起分级级别可配置，编码不再局限于内置三种，
 * 因此收敛为 `string`；权威词表由 `listClassificationConfig()` 提供。
 */
export type DgSensitivity = string;
/** 分类字段的来源：资产级覆盖 / 知识库级 / 租户级 / 未设置。 */
export type DgClassificationSource = 'asset' | 'kb' | 'tenant' | 'none';
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
  };
  truncated: {
    entities: boolean;
    relations: boolean;
    chunks: boolean;
  };
}
