/** Version-pinned graph contract shared with MCP. All names match the wire. */
import { getData, request } from './request';
import { getDocumentViewTicket } from './domainKnowledge';
export type WikiGraphFamily = 'semantic' | 'navigation' | 'provenance';
export interface WikiGraphNode {
  node_id: string; node_type: string; label: string; description: string;
  page_path?: string; document_id?: string; claim_id?: string; aliases?: string[];
  scope?: Record<string, unknown>; fields?: string[]; support_status?: string;
  category?: string;
  file_name?: string; mime_type?: string;
  content?: string; asset_id?: string; pdf_page?: number;
}
export interface WikiGraphEdge {
  edge_id: string; source_node_id: string; target_node_id: string;
  family: WikiGraphFamily; predicate: string; support_status: string;
  qualifiers: Record<string, unknown> | Record<string, string>[];
  description: string; direction: 'in' | 'out'; node: WikiGraphNode; evidence_count: number;
  conflict?: boolean;
}
export interface WikiGraphEvidence {
  evidence_id: string; claim_id: string; page_path: string; document_id: string;
  file_name?: string; mime_type?: string; passage_id: string; chunk_id: string;
  source_sha256: string; start_char: number | null; end_char: number | null;
  excerpt: string; claim_text: string; support_status: string; pdf_page?: number;
}
export interface WikiGraphPage<T> {
  knowledge_base_id: string; graph_revision: string; items: T[]; total: number;
  returned: number; has_more: boolean; next_cursor: string | null;
}
export interface WikiGraphTypes {
  knowledge_base_id: string; graph_revision: string;
  items: { node_type: string; count: number }[];
}
export interface WikiGraphScope { graph_revision?: string; document_id?: string }
function read<T>(path: string, kbId: string, options: object) {
  return getData<T>(request.get(`/knowledge-base/wiki/graph/${path}`, {
    params: { knowledge_base_id: kbId, ...options }, paramsSerializer: { indexes: null },
  }));
}
export const getWikiGraphTypes = (kbId: string, scope: WikiGraphScope = {}) => read<WikiGraphTypes>('types', kbId, scope);
export const searchWikiGraphNodes = (kbId: string, options: WikiGraphScope & { query?: string; node_type?: string; limit?: number; cursor?: string }) => read<WikiGraphPage<WikiGraphNode>>('nodes', kbId, options);
export const getWikiGraphNode = (kbId: string, nodeId: string, revision: string, documentId = '', includeContent = false) => read<{ node: WikiGraphNode; graph_revision: string }>('node', kbId, { node_id: nodeId, graph_revision: revision, document_id: documentId, include_content: includeContent });
export const getWikiGraphNeighbors = (kbId: string, options: WikiGraphScope & { node_id: string; direction?: 'in' | 'out' | 'both'; families?: WikiGraphFamily[]; limit?: number; cursor?: string }) => read<WikiGraphPage<WikiGraphEdge>>('neighbors', kbId, options);
export const getWikiGraphEvidence = (kbId: string, options: WikiGraphScope & { node_id?: string; edge_id?: string; evidence_id?: string; limit?: number; cursor?: string }) => read<WikiGraphPage<WikiGraphEvidence>>('evidence', kbId, options);

/** Reuse the document viewing ticket; the asset route verifies bundle membership. */
export async function getWikiImageUrl(kbId: string, documentId: string, revision: string, assetId: string) {
  const ticket = await getDocumentViewTicket(documentId);
  const token = new URL(ticket.url, window.location.origin).searchParams.get('token');
  if (!token) throw new Error('Image viewing ticket unavailable');
  const params = new URLSearchParams({ knowledge_base_id: kbId, document_id: documentId, graph_revision: revision, token });
  return `/api/v1/knowledge-base/wiki/media/${encodeURIComponent(assetId)}?${params}`;
}
