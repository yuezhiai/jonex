/** Immutable source resources for teaching, records and media feature layers. */
import { getData, request } from './request';

export type WikiResourceKind = 'document' | 'table' | 'page' | 'section' | 'question'
  | 'subquestion' | 'answer' | 'concept' | 'media_segment' | 'video_frame' | 'image';
export interface WikiResource {
  object_id: string; kind: WikiResourceKind; label: string; document_id: string;
  source_sha256: string; content_generation: number; platform_content_generation?: number;
  edition?: string; curriculum?: string; locator: Record<string, unknown>;
  evidence_ids: string[]; parent_id?: string; review_status: 'source' | 'candidate' | 'accepted' | 'rejected';
  table_id?: string; local_number?: string; evidence_kind?: string;
}
export interface WikiResourceReply {
  revision: string; review_version: number; gaps: { document_id: string; reason: string }[];
}
export interface WikiEvidence {
  evidence_id: string; document_id: string; object_id: string; path: string;
  body_sha256: string; start_char: number; end_char: number; text: string;
  text_offset: number; has_more: boolean; next_cursor?: string | null;
  evidence_kind: string; locator: Record<string, unknown>;
}
export interface WikiResourcePage extends WikiResourceReply {
  objects: WikiResource[]; total: number; has_more: boolean; next_cursor?: string | null;
  search_coverage?: { mode: string; budget_limited: boolean };
}
export interface WikiRecordFilter {
  column: string; operator: 'equals' | 'in' | 'contains' | 'not_empty'; value?: string | string[];
}
export interface WikiRecordCell {
  row_index: number; column_index: number; coordinate: string; raw_value: string | null;
  raw_type: string; formula: string | null; cached_value: string | null;
  number_format: string | null; empty_kind: string | null;
}
function read<T>(kbId: string, operation: string, options: object) {
  return getData<T>(request.post(`/knowledge-base/wiki/resources/${operation}`, options,
    { params: { knowledge_base_id: kbId } }));
}
export const findWikiResources = (kbId: string, options: {
  query?: string; kind?: WikiResourceKind; document_id?: string; edition?: string;
  related_to?: string; reviewed_only?: boolean; revision?: string; cursor?: string; limit?: number;
} = {}) => read<WikiResourcePage>(kbId, 'find_resources', options);
export const getWikiQuestion = (kbId: string, revision: string, objectId: string) =>
  read<WikiResourceReply & { question: WikiResource; children: WikiResource[]; evidence: WikiEvidence[]; relations: Record<string, unknown>[] }>(
    kbId, 'get_question', { revision, object_id: objectId });
export const getWikiResourceEvidence = (kbId: string, revision: string, evidenceId: string, cursor = '') =>
  read<WikiResourceReply & { evidence: WikiEvidence }>(kbId, 'get_evidence', { revision, evidence_id: evidenceId, cursor });
export const lookupWikiRecords = (kbId: string, options: {
  revision: string; table_id: string; filters?: WikiRecordFilter[]; filter_groups?: WikiRecordFilter[][];
  include_hidden?: boolean; limit?: number; cursor?: string;
}) => read<WikiResourceReply & {
  records: { row_id: number; document_id: string; table_id: string; cells: WikiRecordCell[] }[];
  total: number; has_more: boolean; next_cursor?: string | null; coverage: Record<string, unknown>;
  fields: Record<string, unknown>[]; context: Record<string, unknown>; observations: Record<string, unknown> | null;
}>(kbId, 'lookup_records', options);
export const getWikiMediaSegment = (kbId: string, revision: string, objectId: string) =>
  read<WikiResourceReply & {
    segment: WikiResource; source: WikiResource; evidence: WikiEvidence[];
    playback: { document_id: string; original_sha256: string; start_seconds: number; end_seconds: number;
      time_precision: 'segment'; word_timing_available: false; url?: string };
  }>(kbId, 'get_media_segment', { revision, object_id: objectId });
export const inspectWikiVisual = (kbId: string, revision: string, objectId: string, question: string, pageNumber?: number) =>
  read<{
    revision: string; object_id: string; document_id: string; asset_sha256: string;
    locator: Record<string, unknown>; model: string; review_status: 'candidate';
    evidence_kind: 'generated_visual_observation'; cached: boolean; limitations: string[];
    result: { observations: string; visible_text: string; uncertainties: string[]; table_candidate: boolean };
  }>(kbId, 'inspect_visual', { revision, object_id: objectId, question, ...(pageNumber === undefined ? {} : { page_number: pageNumber }) });
