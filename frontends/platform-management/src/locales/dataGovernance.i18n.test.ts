/**
 * 资产目录 i18n 防漂移测试（第 2 期数据治理）。
 *
 * 两类静默故障：
 * 1. 中英叶子键集合漂移 —— 页面某文案在一种语言下显示成原始 key；
 * 2. hosted 菜单只认共享资源（`@jonex/i18n-resources`），平台侧加了
 *    `navigation.dataGovernance` 却忘了共享侧 → hosted 侧栏出现裸 key。
 *
 * 因此这里分别校验：平台 zh↔en、共享 zh↔en 的叶子键集合完全一致，
 * 以及 `navigation.dataGovernance` 在两处、两种语言下都能解析到预期文案。
 */
import { describe, expect, it } from 'vitest';
import platformZh from './zh.json';
import platformEn from './en.json';
import sharedZh from '../../../shared/i18n-resources/src/locales/zh.json';
import sharedEn from '../../../shared/i18n-resources/src/locales/en.json';
import {
  DG_ASSET_TYPES,
  DG_DATA_SOURCE_STATUSES,
  DG_DOCUMENT_STATUSES,
} from '../pages/DataGovernance/constant';

type Json = Record<string, unknown>;

/** 页面源码（raw），用于抓取代码中实际引用的 i18n key；测试自身不在扫描范围。 */
const pageSources = import.meta.glob(
  ['../pages/DataGovernance/**/*.{ts,tsx}', '!../pages/DataGovernance/**/*.test.*'],
  {
    query: '?raw',
    import: 'default',
    eager: true,
  },
) as Record<string, string>;

/** 点分路径取值；任一层缺失返回 undefined（叶子值为空串也算存在）。 */
function resolveKey(node: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (acc === null || typeof acc !== 'object' || Array.isArray(acc)) return undefined;
    return (acc as Json)[part];
  }, node);
}

/**
 * 抓源码中的静态 `dataGovernance.*` key。
 *
 * 模板串（如 `` `dataGovernance.sensitivity.${value}` ``）会抓到以 `.` 结尾的
 * 动态前缀，这类不是完整 key，过滤掉。
 */
function extractStaticKeys(source: string): string[] {
  return (source.match(/\bdataGovernance\.[A-Za-z0-9_.]+/g) ?? []).filter(
    (key) => !key.endsWith('.'),
  );
}

/** 抓 `t('namespace.key')` 形式的字面量 key（只认页面会用的两个命名空间）。 */
function extractTCallKeys(source: string): string[] {
  return Array.from(
    source.matchAll(/\bt\(\s*['"]([A-Za-z0-9_.]+)['"]/g),
    (match) => match[1],
  ).filter((key) => key.startsWith('dataGovernance.') || key.startsWith('common.'));
}

/**
 * 动态拼接的 key 家族：模板串无法从源码静态还原，这里按权威词表展开，
 * 保证 `status.${group}.${status}` 这类分支没有漏翻。
 */
const DYNAMIC_KEYS: string[] = [
  ...DG_ASSET_TYPES.map((value) => `dataGovernance.assetType.${value}`),
  // 内置级别文案：levelLabel 对未改名的内置 code 走 `sensitivity.*`。
  // DG_SENSITIVITIES 已随第 4 期配置化移除，这里保留最小内置词表。
  ...['public', 'internal', 'confidential'].map(
    (value) => `dataGovernance.sensitivity.${value}`,
  ),
  'dataGovernance.sensitivity.unclassified',
  ...DG_DOCUMENT_STATUSES.map((value) => `dataGovernance.status.document.${value}`),
  ...DG_DATA_SOURCE_STATUSES.map((value) => `dataGovernance.status.dataSource.${value}`),
  ...['ok', 'suspect', 'bad', 'unknown'].map((value) => `dataGovernance.quality.${value}`),
  // 保留策略来源：`retentionSourceLabelKey` 是模板串，必须逐值展开校验。
  ...['document', 'kb', 'tenant', 'system'].map(
    (value) => `dataGovernance.retention.source.${value}`,
  ),
  // 分类字段来源：`classificationSourceLabelKey` 同为模板串。
  // `table` = 列继承表级覆盖，必须与其余来源一起校验。
  ...['asset', 'table', 'kb', 'tenant', 'none'].map(
    (value) => `dataGovernance.classification.source.${value}`,
  ),
  // compiled schema 同步状态：`syncStatusText` 用模板串拼接。
  ...['synced', 'outdated', 'apply_failed'].map(
    (value) => `dataGovernance.syncStatus.${value}`,
  ),
  // 标准资产状态：`statusText` / 列表 Tag 都用模板串拼接。
  ...['draft', 'published', 'retired'].map(
    (value) => `dataGovernance.standardAssets.status.${value}`,
  ),
  // 数据合同状态：`statusLabelKey` 返回模板串、列表与抽屉 Tag 共用。
  ...['draft', 'published', 'retired'].map(
    (value) => `dataGovernance.dataContracts.status.${value}`,
  ),
  // W2.5 质量闭环：以下 key 家族全部由模板串拼接（列表 Tag / 筛选下拉 / 摘要）。
  ...['completeness', 'uniqueness', 'validity', 'consistency', 'timeliness'].map(
    (value) => `dataGovernance.quality.dimension.${value}`,
  ),
  ...['info', 'warning', 'error', 'critical'].map(
    (value) => `dataGovernance.quality.severity.${value}`,
  ),
  ...['library', 'sql', 'custom'].map(
    (value) => `dataGovernance.quality.checkType.${value}`,
  ),
  ...['open', 'assigned', 'fixing', 'pending_verify', 'closed'].map(
    (value) => `dataGovernance.quality.issueStatus.${value}`,
  ),
  ...['running', 'completed', 'failed'].map(
    (value) => `dataGovernance.quality.runStatus.${value}`,
  ),
  ...['passed', 'failed', 'error'].map(
    (value) => `dataGovernance.quality.resultStatus.${value}`,
  ),
  // 内置规则指标与工单事件：`metricLabelKey` / `issueEventLabelKey` 模板串。
  ...['rowCount', 'nullValues', 'missingValues', 'duplicateValues', 'invalidValues', 'unique'].map(
    (value) => `dataGovernance.quality.metric.${value}`,
  ),
  ...['created', 'assigned', 'status_change', 'verified', 'reopened', 'comment'].map(
    (value) => `dataGovernance.quality.issueEvent.${value}`,
  ),
  // W2.6 资源视图：以下 key 家族全部由模板串拼接（资产类型 / 分级 / 绑定状态 /
  // 绑定来源 / 责任分组）。
  ...['table', 'column', 'metric', 'document', 'dataSource'].map(
    (value) => `dataGovernance.resourceViews.assetType.${value}`,
  ),
  ...['public', 'internal', 'confidential', 'secret', 'unclassified'].map(
    (value) => `dataGovernance.resourceViews.sensitivity.${value}`,
  ),
  ...['active', 'disabled'].map(
    (value) => `dataGovernance.resourceViews.bindingStatus.${value}`,
  ),
  ...['manual', 'suggested'].map(
    (value) => `dataGovernance.resourceViews.bindingSource.${value}`,
  ),
  ...['owner', 'domain'].map(
    (value) => `dataGovernance.resourceViews.responsibility.groupBy.${value}`,
  ),
];

/** 递归收集叶子键（点分路径）；数组与 null 视作叶子。 */
function leafKeys(node: Json, prefix = ''): string[] {
  return Object.entries(node).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return leafKeys(value as Json, path);
    }
    return [path];
  });
}

function missingKeys(source: string[], target: string[]): string[] {
  const targetSet = new Set(target);
  return source.filter((key) => !targetSet.has(key));
}

describe('资产目录 i18n 叶子键对齐', () => {
  it('平台中英叶子键集合一致', () => {
    const zh = leafKeys(platformZh as Json);
    const en = leafKeys(platformEn as Json);
    expect(missingKeys(zh, en)).toEqual([]);
    expect(missingKeys(en, zh)).toEqual([]);
  });

  it('共享资源中英叶子键集合一致', () => {
    const zh = leafKeys(sharedZh as Json);
    const en = leafKeys(sharedEn as Json);
    expect(missingKeys(zh, en)).toEqual([]);
    expect(missingKeys(en, zh)).toEqual([]);
  });

  it('navigation.dataGovernance 在平台与共享资源中均可解析', () => {
    // 平台侧（standalone 侧栏）：title 存的是 key，靠资源解析成文案。
    expect((platformZh as any).navigation?.dataGovernance).toBe('资产目录');
    expect((platformEn as any).navigation?.dataGovernance).toBe('Asset Catalog');
    // 共享侧（hosted 菜单）：路由菜单只读共享资源。
    expect((sharedZh as any).navigation?.dataGovernance).toBe('资产目录');
    expect((sharedEn as any).navigation?.dataGovernance).toBe('Asset Catalog');
  });

  it('平台 dataGovernance 命名空间在两种语言下都完整', () => {
    const zh = (platformZh as any).dataGovernance;
    const en = (platformEn as any).dataGovernance;
    expect(zh).toBeTruthy();
    expect(en).toBeTruthy();
    expect(zh.title).toBe('资产目录');
    expect(en.title).toBe('Asset Catalog');
    expect(zh.quality.suspect).toBeTruthy();
    expect(en.quality.suspect).toBeTruthy();
  });

  it('页面源码引用的静态/动态 key 在中英下都存在且已翻译', () => {
    const sources = Object.values(pageSources);
    expect(sources.length).toBeGreaterThan(0);

    const keys = new Set([
      ...sources.flatMap(extractStaticKeys),
      ...sources.flatMap(extractTCallKeys),
      ...DYNAMIC_KEYS,
    ]);
    expect(keys.size).toBeGreaterThan(0);

    const problems: string[] = [];
    for (const key of keys) {
      for (const [locale, messages] of [
        ['zh', platformZh],
        ['en', platformEn],
      ] as const) {
        const value = resolveKey(messages, key);
        if (typeof value !== 'string') {
          problems.push(`${locale}:${key} 缺失`);
        } else if (value === key) {
          // 复制 key 冒充翻译会让界面直接显示裸 key，同样算漂移。
          problems.push(`${locale}:${key} 未翻译`);
        }
      }
    }
    expect(problems).toEqual([]);
  });
});
