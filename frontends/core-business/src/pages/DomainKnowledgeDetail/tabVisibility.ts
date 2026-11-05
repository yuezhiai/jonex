/**
 * 知识库详情页页签可见性。
 *
 * 「结构化」是知识库创建时的开关（schema / 血缘 / 治理等结构化能力的前提）：
 * 关闭后这些页签既不显示，也不应成为 activeTab —— 否则会渲染出一个用户点不到的页面。
 *
 * ⚠️ `datasource`（数据源）**不在此列**：数据源接入（文件上传 / API 拉取 / 对象存储 /
 * 入站推送）是知识库的基础能力，与「结构化数据支持」无关，任何知识库都要能接数据。
 * 该页签恒定展示，默认 activeTab 也仍是它。
 */
export const STRUCTURED_TAB_KEYS = new Set([
  'schema',
  'abstractSchema',
  'lineage',
  'etl',
  'governance',
]);

export function visibleTabs<T extends { key: string }>(
  tabs: T[],
  structuredEnabled: boolean,
): T[] {
  return structuredEnabled ? tabs : tabs.filter((t) => !STRUCTURED_TAB_KEYS.has(t.key));
}

export function resolveActiveTab(
  active: string,
  tabs: { key: string }[],
  structuredEnabled: boolean,
): string {
  const visible = visibleTabs(tabs, structuredEnabled);
  if (visible.some((t) => t.key === active)) return active;
  return visible[0]?.key ?? active;
}
