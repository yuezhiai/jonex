/**
 * 属性来源（`Attribute.sources`）的展示模型 —— 纯函数，单独成模块以便单测。
 *
 * 属性来源 = 该属性由哪些**源实体列**贡献（随 v2 往返保真；编辑器不提供增删，
 * 由生成/对齐产生 —— spec §五「属性级来源展示（可空）」）。标签回落链与
 * ConceptCard 的 `sourceLabel` 同口径：源名（落点里带）→ 源已删用「已被删除」
 * 文案替代 id → 否则 raw `dataSourceId`（可拿去对账）。
 */
import type { AttributeSource, EntityLanding } from '../domain/index.js';

export interface AttributeSourceView {
  /** 源显示名（从所属实体的落点里按 dataSourceId 解析 `sourceName`）；解析不到 = undefined */
  sourceName?: string;
  /** 源已被删（source_gone）—— 源名缺失时界面用「该数据源已被删除」替代 id */
  sourceGone: boolean;
  /** 永远可显示的兜底（raw id） */
  dataSourceId: string;
  /** 源里的「实体.列」定位串 */
  column: string;
  /** 落点角色（自由文本；没给 = undefined）。 */
  role?: string;
  stale: boolean;
  staleReason?: string | null;
}

/** 单条属性来源 → 展示模型。`landings` 传**所属实体**的落点（属性来源的源必是它的落点之一或已删源）。 */
export function toAttributeSourceView(
  source: AttributeSource,
  landings: readonly EntityLanding[],
): AttributeSourceView {
  const landing = landings.find((item) => item.dataSourceId === source.dataSourceId);
  return {
    sourceName: landing?.sourceName ?? undefined,
    sourceGone: source.staleReason === 'source_gone',
    dataSourceId: source.dataSourceId,
    column: `${source.entityName}.${source.attributeName}`,
    role: source.role,
    stale: source.stale === true,
    staleReason: source.staleReason,
  };
}
