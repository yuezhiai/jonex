import type { DataLineageBlock } from '@/types/knowledgeSearch';

/** Whether the lineage block has anything to render (absent / empty items => false). */
export function hasLineage(block?: DataLineageBlock | null): boolean {
  return Boolean(block && Array.isArray(block.items) && block.items.length > 0);
}

/** Render model for one item: counts and the explicit unknown flag. */
export interface LineageItemSummary {
  entityType: string;
  dataSourceId: string;
  table: string;
  upstream: number;
  downstream: number;
  unknown: boolean;
}

/**
 * Pure projection shared by the view and tests.
 * `unknown` is preserved verbatim: the UI must render it explicitly
 * ("could not be read" is not the same as "none").
 */
export function lineageSummary(block?: DataLineageBlock | null): LineageItemSummary[] {
  if (!hasLineage(block)) return [];
  return (block as DataLineageBlock).items.map((item) => ({
    entityType: item.entity_type ?? '',
    dataSourceId: item.data_source_id ?? '',
    table: item.table ?? '',
    upstream: Array.isArray(item.upstream) ? item.upstream.length : 0,
    downstream: Array.isArray(item.downstream) ? item.downstream.length : 0,
    unknown: item.unknown === true,
  }));
}
