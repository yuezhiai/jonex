/**
 * 数据运营模块的类型定义。
 *
 * 页面、组件与取数层（api/dataOperations.ts）共用这一份 —— 组件只认这里的形状，
 * 不关心数据来自常量还是后端接口。
 */

/**
 * 资产类型 —— 决定 tag 配色与文案 key。
 *
 * `other` 是后端 8 类分类里落不进前 7 类的那一档（契约要求前端留个展示位），
 * 也是后端 `asset_type` 出现未知值时的兜底 —— 宁可在页面上标「其他」，
 * 也不要把判不出来的资产悄悄混进「文档」。
 *
 * `text`（纯文本，.txt/.md/.markdown）是后端新拆出来的一类，**必须与前端的
 * `ASSET_TYPES` 同步加**：`toAssetType()` 对不认识的 `asset_type` 一律回落
 * `other`，漏加会让所有 .txt 资产在榜单/详情里静默变成「其他」。
 */
export type DoAssetType = 'document' | 'text' | 'table' | 'image' | 'structured' | 'video' | 'audio' | 'other';

/** 处理阶段 —— 数据处理流水与异常资产共用 */
export type DoStage = 'parse' | 'extract' | 'compile';

/** 分析对象口径 —— 知识资产页的分段器 */
export type DoScope = 'ontology' | 'wiki';

/**
 * KPI 卡。value 存已格式化的展示串（千分位）：这些数字来自不同接口、
 * 格式规则也不完全一致（如「6/9」这种比值），在数据层定稿比在每个页面各写一遍 toLocaleString 稳。
 */
export interface DoKpiCard {
  key: string;
  labelKey: string;
  value: string;
  /** 强调色，用于图标底色 */
  accent: string;
  /** 图标标识，映射表在 components/StatCards.tsx */
  iconKey: string;
}

/** 趋势图数据点 */
export interface DoTrendPoint {
  date: string;
  value: number;
}

/**
 * 占比条条目。percent 不在这里存：占比一律由 count 与同组总量算出（见 ProportionList），
 * 免得常量里的数字与 count 对不上。口径不同的比率（如检索参与率）另用专门类型。
 */
export interface DoProportionItem {
  key: string;
  /** i18n 文案 key；知识库名这类不需要翻译的走 label */
  labelKey?: string;
  label?: string;
  count: number;
  /**
   * 该行的条颜色。不传则按数组下标取 `ProportionList` 的共享色板。
   *
   * 资产构成图**必须显式给色**：它只展示五类、而色板共享给另外三张占比图
   * （知识资产分布 / 检索方式 / 热门话题），调色板顺序一改那三处会跟着漂。
   */
  color?: string;
}

export interface DoHotAsset {
  id: string;
  name: string;
  assetType: DoAssetType;
  kbName: string;
  /** 所属知识库 id，「前往知识库」的跳转目标。后端富化不到时为 null */
  kbId: string | null;
  refCount: number;
  lastRefAt: string;
}

export interface DoIdleAsset {
  id: string;
  name: string;
  assetType: DoAssetType;
  kbName: string;
  /** 所属知识库 id，「前往知识库」的跳转目标。后端富化不到时为 null */
  kbId: string | null;
  /** null 表示从未被引用 */
  lastRefAt: string | null;
  updatedAt: string;
}

/** 数据处理流水概览：四级流水线的四个大数字 */
export interface DoPipelineOverview {
  total: number;
  parsed: number;
  extracted: number;
  compiled: number;
}

export type DoTaskStatus = 'success' | 'failed' | 'running' | 'pending';

/**
 * 任务状态分布与平均耗时。
 *
 * ⚠️ 这几个数**必须取后端的 `pipeline_task_stats`**，不能拿任务明细现数：
 * 明细是按开始时间倒序的 TOP-20，数出来的是「最近 20 条里的分布」，
 * 任务一多就会把成功数少算成 20 以内。
 */
export interface DoPipelineStats {
  success: number;
  failed: number;
  running: number;
  pending: number;
  /** 仅成功任务参与；没有成功任务、或成功任务两端时间不齐时为 null */
  avgDurationMs: number | null;
}

/** 异常资产汇总：总数 + 分阶段失败数 */
export interface DoAnomalySummary {
  total: number;
  byStage: { stage: DoStage; count: number }[];
}

/**
 * 异常资产（= 失败的处理任务）。
 *
 * 后端还会给 `detail`（抽屉详情，键对齐 `dataOperations.anomaly.field.*`）与
 * `handled` / `kbName` / `retryCount`，那是异常下钻抽屉的字段；抽屉还没做，
 * 这里只保留摘要卡用得到的那几个。哪天做下钻再把它们加回来。
 */
export interface DoAnomalyAsset {
  id: string;
  assetName: string;
  stage: DoStage;
  reason: string;
  occurredAt: string;
}

/** 解析质量异常（观测档）：最近 `verdict='bad'` 的摄取任务。 */
export interface DoQualityAnomaly {
  documentId: string;
  documentName: string;
  kbId: string;
  kbName: string;
  verdict: string;
  /** 判据名（如 page_yield_zero），由前端映射为文案。 */
  reasons: string[];
  occurredAt: string;
}

export interface DoKnowledgeKpi {
  key: string;
  labelKey: string;
  value: string;
  /** 「近 7 天新增」的数值部分，前缀走 i18n 的 kpi.newIn7d */
  deltaText: string;
  accent: string;
  iconKey: string;
}

/** 分析对象切换时右侧的两个活跃度指标 */
export interface DoScopeActivity {
  /** 近 7 天活跃实例数 */
  activeCount: number;
  /** 实例活跃度百分数，如 33.58 */
  activeRate: number;
}

export interface DoHotInstance {
  /**
   * 展示用 id（= 后端 `object_id`，`sha256(tenant|kb|type|name)` 的单向哈希）。
   * ⚠️ **查不了详情** —— 反解不回三元组，改名后历史哈希还会指向不存在的键。
   * 只当 React key 用；下钻抽屉要的是 `kbId` + `objectType` + `name` 三元组。
   */
  id: string;
  name: string;
  objectType: string;
  kbName: string;
  /** 所属知识库 id，抽屉 B 的定位键之一。后端富化不到时为 null（此时该行不可下钻） */
  kbId: string | null;
  hitCount: number;
  adoptCount: number;
  lastUsedAt: string;
}

export interface DoIdleKnowledge {
  /** 展示用 id（= 后端 `object_id` 哈希）—— 同 `DoHotInstance.id`，只当 React key，不能查详情 */
  id: string;
  name: string;
  objectType: string;
  kbName: string;
  /** 所属知识库 id，抽屉 B 的定位键之一。后端富化不到时为 null（此时该行不可下钻） */
  kbId: string | null;
  /** null 表示从未使用 */
  lastUsedAt: string | null;
  updatedAt: string;
}

/** 检索方式。参与率不等于占比（同一请求可同时走多条链路），故单独给字段 */
export interface DoSearchMode {
  key: 'ontology' | 'wiki' | 'rag';
  labelKey: string;
  count: number;
  participationRate: number;
}

export interface DoHotQuestion {
  rank: number;
  question: string;
  /** 提问主题的 i18n key —— 后端没这个字段（主题聚类是三期），这一列必然为空 */
  topicKey: string;
  count: number;
}

export interface DoUsageItem {
  id: string;
  name: string;
  /** 仅知识库使用情况有类型列；领域服务不传 */
  typeKey?: string;
  count: number;
  lastUsedAt: string;
}

/** 知识库下拉的可选项 —— 只带 id 与库名，够 Select 用 */
export interface DoKbOption {
  id: string;
  name: string;
}

/* ══════════════════════ 列表钻取：时间窗口 ══════════════════════ */

/**
 * 统计窗口。与后端约定的两种形态：
 * - `{ days }` —— 相对窗口（预设「近 N 天」），对应 query `days`；
 * - `{ startDate, endDate }` —— 自定义闭区间（`YYYY-MM-DD`），对应 `start_date`/`end_date`，成对给出时覆盖 `days`。
 *
 * 趋势卡选中的窗口会驱动**本页的趋势图、喂抽屉的列表与四个抽屉** —— 列表与抽屉共用同一个
 * 窗口，是「抽屉数字 == 列表那一行」的唯一保证（见 api/dataOperations.ts 的 windowParams）。
 */
export type DoWindow = { days: number } | { startDate: string; endDate: string };

/* ══════════════════════ 列表钻取：抽屉定位键 ══════════════════════ */

/** 抽屉 A 的定位键。`kbId` 传了做交叉校验（该资产不属于此库即 404），可为 null */
export interface DoAssetTarget {
  assetId: string;
  kbId: string | null;
}

/** 抽屉 B 的定位键 —— 三元组，**不是**列表里的 `object_id` */
export interface DoEntityTarget {
  kbId: string;
  entityType: string;
  canonicalName: string;
}

/* ══════════════════════ 列表钻取：抽屉 A · 数据资产详情 ══════════════════════ */

/**
 * 数据资产详情。
 *
 * ⚠️ 「处理状态」在后端**没有单一字段** —— 解析/本体/LlmWiki 三条链各有自己的状态码，
 * 这里原样透出三个（见 §三）。非 wiki 库 `llmWikiCompileStatus` 恒 null（不是缺失）。
 */
export interface DoAssetDetail {
  assetId: string;
  fileName: string;
  assetType: DoAssetType | null;
  /** 展示用格式串，后端已算好（扩展名大写）；无扩展名时兜底 mime 子类型 */
  fileFormat: string | null;
  /** 字节 */
  fileSize: number | null;
  /** 数据来源原始码：file / api / api_push / storage，需 i18n 映射 */
  source: string | null;
  kbId: string | null;
  kbName: string | null;
  uploadedAt: string | null;
  /** 文档行的最后变更时刻 */
  lastProcessedAt: string | null;
  /** 解析/入库链路：pending / parsing / ingesting / ready / failed */
  status: string | null;
  /** 本体抽取：pending / extracting / ready / failed */
  ontologyStatus: string | null;
  /** LLM-Wiki 编译：compiling / compiled / stale / failed；非 wiki 库恒 null */
  llmWikiCompileStatus: string | null;
  /** 窗口内被引用次数（与「高频引用资产」那一行同口径同窗口） */
  citeCount: number;
  /** 最近被引用时间；从未被引用 → null */
  lastCitedAt: string | null;
}

/* ══════════════════════ 列表钻取：抽屉 B · Ontology 对象详情 ══════════════════════ */

/** 关系目标。方向不暴露：正反向边被后端合并进同一组 */
export interface DoEntityRelationTarget {
  canonicalName: string;
  /** 后端可空 —— 属性缺失的邻居实体没有类型 */
  entityType: string | null;
}

export interface DoEntityRelationGroup {
  relationType: string;
  targets: DoEntityRelationTarget[];
}

/** 图里原样存着的属性；value 统一转成字符串，后端不补默认字段 */
export interface DoEntityAttribute {
  key: string;
  value: string;
}

export interface DoEntityDetail {
  kbId: string;
  kbName: string | null;
  canonicalName: string;
  entityType: string;
  aliases: string[];
  description: string | null;
  /** Neo4j 来源 → 偏移量 +08:00 */
  createdAt: string | null;
  updatedAt: string | null;
  attributes: DoEntityAttribute[];
  /** 全部关系数（不是 relationGroups 展开后的条数） */
  relationTotal: number;
  relationGroups: DoEntityRelationGroup[];
  /** 关系明细被 200 条上限截断 */
  relationTruncated: boolean;
  /** 窗口内命中次数（与「高频使用本体实例」那一行同口径同窗口） */
  hitCount: number;
  /** 结构性恒 0 —— 采用事件只对文档建，实体没有采用锚点 */
  adoptedCount: number;
  lastUsedAt: string | null;
  /** Neo4j 不可用 → true，同时 attributes/relationGroups 为空、relationTotal=0；但 hitCount 照常 */
  degraded: boolean;
}

/* ══════════════════════ 列表钻取：抽屉 C · 知识库详情 ══════════════════════ */

/**
 * 本体计数。⚠️ `null` = 取不到（降级）、`0` = 库里真的是 0 —— 两者必须分开渲染。
 * `degraded` 为 true 时上面两个一定是 null；反之 null 必然伴随 true。
 */
export interface DoKbOntologyCount {
  objectInstances: number | null;
  relationInstances: number | null;
  degraded: boolean;
}

export interface DoKbDetail {
  kbId: string;
  kbName: string;
  /** 真实取值 lightrag / openkb / jonex_wiki（原型上的 Ontology/Wiki 是展示名）；后端可空 */
  kbType: string | null;
  ontology: DoKbOntologyCount;
  /** READY 文档数（= assetsByType 各项之和） */
  assetTotal: number;
  /** ⚠️ 只含非零项、长度不固定 —— 与页1 composition 的定长 7 项形状不同 */
  assetsByType: { assetType: DoAssetType; count: number }[];
  /** 该库关联的领域服务；serviceName 取不到时回落 serviceId */
  services: { serviceId: string; serviceName: string }[];
  updatedAt: string | null;
  /** 窗口内该库的检索请求数（与「知识库使用情况」那一行同口径同窗口） */
  requestCount: number;
  lastRequestAt: string | null;
}

/* ══════════════════════ 列表钻取：抽屉 D · 领域服务详情 ══════════════════════ */

/**
 * 领域服务详情。
 *
 * ⚠️ 本接口**任何情况都 200**（与 A/B/C 的四合一 404 有意不同）：列表行来自「事件归因」，
 * 服务实体查不到时列表本身就回落成 id 显示，所以「行在、实体不在」是合法状态。此时
 * `serviceName` 回落成你传的 `serviceId`、`status`/`enabled` 为 null、`knowledgeBases` 为 []，
 * **使用情况照常返回**。前端不要给它写「不存在」的报错分支。
 */
export interface DoServiceDetail {
  serviceId: string;
  serviceName: string;
  status: string | null;
  /** null = 不知道（不是「已停用」，停用是 false） */
  enabled: boolean | null;
  knowledgeBases: { kbId: string; kbName: string }[];
  /** 窗口内该服务的调用次数（与「领域服务使用情况」那一行同口径同窗口） */
  requestCount: number;
  lastRequestAt: string | null;
}
