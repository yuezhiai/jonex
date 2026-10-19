/**
 * 引用 → source-spans 查询参数的纯函数（引用 bbox 定位）。
 *
 * 引用位置里的 page_no/table_idx/row_start/row_end/image_idx 用于让后端在
 * 同一 chunk 的多个 span 中挑出最贴合该引用的那一段；chunk_index 定位候选 chunk。
 */
import type { KnowledgeReferenceLocation } from '@/types/knowledgeSearch';

export interface SpanExpect {
  page_no?: number;
  table_idx?: number;
  row_start?: number;
  row_end?: number;
  image_idx?: number;
}

/** 引用位置 → `getDocumentSourceSpans` 入参；空值字段一律丢弃。 */
export function toSpanRequest(
  loc?: KnowledgeReferenceLocation,
): { chunkIndex: number | null; expect: SpanExpect } {
  const expect: SpanExpect = {};
  if (loc?.page_no != null) expect.page_no = loc.page_no;
  if (loc?.table_idx != null) expect.table_idx = loc.table_idx;
  if (loc?.row_start != null) expect.row_start = loc.row_start;
  if (loc?.row_end != null) expect.row_end = loc.row_end;
  if (loc?.image_idx != null) expect.image_idx = loc.image_idx;
  return { chunkIndex: loc?.chunk_index ?? null, expect };
}
