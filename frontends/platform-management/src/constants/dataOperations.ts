/**
 * 数据运营模块的常量数据。
 *
 * 分两类，价值不同，清理时不要一刀切：
 *   ① 只当「壳」用的 —— KPI 卡的 labelKey/iconKey 活着，value 会被真实接口覆盖，这里的值只是兜底；
 *   ② 行定义 —— 构成图的展示位（key + 文案），count 同样会被真实接口覆盖。
 *
 * 原本还有一类「后端没有对应接口、常量就是数据来源的」（流水概览、异常资产、按来源构成），
 * 三块随卡片整段注释保留过 —— 流水与异常接上二期埋点后已删；「按来源构成」是**三期能力**，
 * 契约里根本没有该字段，不留桩位。再往这里加模拟数据前，先确认没有接口能取。
 *
 * 日期统一 `YYYY-MM-DD HH:mm`（与 formatDateTime 输出一致），趋势图的 X 轴用 `MM-DD`。
 */

import type { DoKnowledgeKpi, DoKpiCard, DoProportionItem, DoSearchMode, DoWindow } from '../pages/DataOperations/types';
import { fmtCount, fmtRate, fmtSigned } from '../pages/DataOperations/utils';

/* ══════════════════════ 数字源 ══════════════════════ */

/* 页 1 */
export const DO_ASSET_TOTAL = 35170;
export const DO_ASSET_SOURCE_COUNT = 38;
export const DO_ASSET_CITED = 13846;
/** 引用率是展示口径：13,846 / 35,170 = 39.3688%，与卡面 39.37% 并非同一算法，不要互相反算 */
export const DO_ASSET_USAGE_RATE = 39.37;

/* 页 2 */
export const DO_KB_OBJECT_TOTAL = 218640;
export const DO_KB_RELATION_TOTAL = 297420;
export const DO_KB_ENTITY_TOTAL = 35680;
export const DO_KB_CONCEPT_TOTAL = 8426;
export const DO_KB_OBJECT_DELTA = 12340;
export const DO_KB_RELATION_DELTA = 18760;
export const DO_KB_ENTITY_DELTA = 2180;
export const DO_KB_CONCEPT_DELTA = 540;
/* 页 3 */
export const DO_SEARCH_REQUESTS = 120840;
export const DO_SEARCH_USERS = 3240;
/** 人均检索次数 = 请求数 / 用户数，两个基数同比例缩放，故它不随空间变化 */
export const DO_SEARCH_PER_USER = 37.3;

/* ══════════════════════ 页 1：数据资产 ══════════════════════ */

export const DO_ASSET_KPIS: DoKpiCard[] = [
  {
    key: 'total',
    labelKey: 'dataOperations.kpi.assetTotal',
    value: fmtCount(DO_ASSET_TOTAL),
    accent: '#3b82f6',
    iconKey: 'database',
  },
  {
    key: 'source',
    labelKey: 'dataOperations.kpi.sourceCount',
    value: fmtCount(DO_ASSET_SOURCE_COUNT),
    accent: '#8b5cf6',
    iconKey: 'source',
  },
  {
    key: 'cited',
    labelKey: 'dataOperations.kpi.citedAssets',
    value: fmtCount(DO_ASSET_CITED),
    accent: '#10b981',
    iconKey: 'link',
  },
  {
    key: 'usageRate',
    labelKey: 'dataOperations.kpi.usageRate',
    value: fmtRate(DO_ASSET_USAGE_RATE),
    accent: '#f59e0b',
    iconKey: 'percent',
  },
];

/**
 * 按数据类型分组 —— 这里是**行的定义**（key、文案、颜色与顺序），count 是占位值：
 * 取数层会用后端返回的 `asset_type` 计数覆盖，页面上的数字不是这里的。
 *
 * ⚠️ **只展示五类是有意的**（文档/文本/图像/音频/视频，与平台「解析器配置」的
 * 文档/文本/图像/音频/视频五档同口径）；表格、结构化数据、其他三类**隐藏**。
 * 注意「隐藏」只作用于展示 —— 它们**仍计入占比分母**（页面口径是「占全部资产」），
 * 所以五类百分比之和小于 100%。分母由取数层单独给（`getAssetComposition` 的
 * `total`），不要指望 `ProportionList` 从这五行里自己加出来。
 *
 * 知识库详情抽屉复用同一份定义（见 KbDetailDrawer），两处的类别、顺序、颜色因此天然一致。
 *
 * 颜色逐个写死、不靠下标：`ProportionList` 的默认色板是共享的，按新顺序取会撞色。
 */
export const DO_ASSET_COMPOSITION: DoProportionItem[] = [
  { key: 'document', labelKey: 'dataOperations.assetType.document', count: 6040, color: '#3b82f6' },
  { key: 'text', labelKey: 'dataOperations.assetType.text', count: 3460, color: '#0d9488' },
  { key: 'image', labelKey: 'dataOperations.assetType.image', count: 1680, color: '#8b5cf6' },
  { key: 'audio', labelKey: 'dataOperations.assetType.audio', count: 420, color: '#ec4899' },
  { key: 'video', labelKey: 'dataOperations.assetType.video', count: 510, color: '#f97316' },
];

/* ══════════════════════ 页 2：知识资产 ══════════════════════ */

/** 四张卡固定展示 Ontology 与 Wiki 的全貌，不随分析对象分段器变化 */
export const DO_KNOWLEDGE_KPIS: DoKnowledgeKpi[] = [
  {
    key: 'object',
    labelKey: 'dataOperations.kpi.ontologyObject',
    value: fmtCount(DO_KB_OBJECT_TOTAL),
    deltaText: fmtSigned(DO_KB_OBJECT_DELTA),
    accent: '#3b82f6',
    iconKey: 'object',
  },
  {
    key: 'relation',
    labelKey: 'dataOperations.kpi.ontologyRelation',
    value: fmtCount(DO_KB_RELATION_TOTAL),
    deltaText: fmtSigned(DO_KB_RELATION_DELTA),
    accent: '#8b5cf6',
    iconKey: 'relation',
  },
  {
    key: 'entity',
    labelKey: 'dataOperations.kpi.wikiEntity',
    value: fmtCount(DO_KB_ENTITY_TOTAL),
    deltaText: fmtSigned(DO_KB_ENTITY_DELTA),
    accent: '#10b981',
    iconKey: 'entity',
  },
  {
    key: 'concept',
    labelKey: 'dataOperations.kpi.wikiConcept',
    value: fmtCount(DO_KB_CONCEPT_TOTAL),
    deltaText: fmtSigned(DO_KB_CONCEPT_DELTA),
    accent: '#f59e0b',
    iconKey: 'concept',
  },
];

/* ══════════════════════ 页 3：检索分析 ══════════════════════ */

/** 近 7 天有检索记录的知识库数 / 平台知识库总数 */
export const DO_SEARCH_KB_COVERED = 6;
export const DO_SEARCH_KB_TOTAL = 9;

export const DO_SEARCH_KPIS: DoKpiCard[] = [
  {
    key: 'requests',
    labelKey: 'dataOperations.kpi.searchRequests',
    value: fmtCount(DO_SEARCH_REQUESTS),
    accent: '#3b82f6',
    iconKey: 'search',
  },
  {
    key: 'kbs',
    labelKey: 'dataOperations.kpi.searchedKb',
    value: `${DO_SEARCH_KB_COVERED}/${DO_SEARCH_KB_TOTAL}`,
    accent: '#8b5cf6',
    iconKey: 'kb',
  },
  {
    key: 'users',
    labelKey: 'dataOperations.kpi.searchUsers',
    value: fmtCount(DO_SEARCH_USERS),
    accent: '#10b981',
    iconKey: 'user',
  },
  {
    key: 'avg',
    labelKey: 'dataOperations.kpi.avgPerUser',
    value: String(DO_SEARCH_PER_USER),
    accent: '#f59e0b',
    iconKey: 'avg',
  },
];

/** 参与率不是占比：同一次检索可同时命中多条链路，故三者之和大于 100% */
export const DO_SEARCH_MODES: DoSearchMode[] = [
  { key: 'ontology', labelKey: 'dataOperations.searchMode.ontology', count: 96840, participationRate: 80.1 },
  { key: 'wiki', labelKey: 'dataOperations.searchMode.wiki', count: 83420, participationRate: 69.0 },
  { key: 'rag', labelKey: 'dataOperations.searchMode.rag', count: 72680, participationRate: 60.1 },
];

// 高频提问的「数据更新至」不再用这个装成更新时间，改为记这次取数的时刻（见 SearchAnalysis）；
// 后端哪天给出真实的更新时间字段，再把这个常量换回来并传进 section.hotQuestionsHint
// export const DO_HOT_QUESTIONS_UPDATED_AT = '09-18 16:00';

/** 知识库筛选的「全部知识库」值 —— 页面与取数层共用，避免两处各写一遍字面量 */
export const DO_KB_ALL = 'all';

/* ══════════════════════ 列表钻取：时间窗口 ══════════════════════ */

/** 默认窗口天数（近 7 天）—— 与 KPI 卡的「近 7 天」同口径 */
export const DO_DEFAULT_WINDOW_DAYS = 7;

/** 默认窗口。趋势、喂抽屉的列表、抽屉都以它起步，改一处即可全页同步 */
export const DO_DEFAULT_WINDOW: DoWindow = { days: DO_DEFAULT_WINDOW_DAYS };

/** 趋势卡区间选择器的预设天数（顺序即下拉里的顺序） */
export const DO_WINDOW_PRESET_DAYS: readonly number[] = [7, 14, 30, 90];
