/**
 * [jonex] Wiki 阅读界面 API（jonex_wiki：目录 → 文章 → 溯源 References）。
 *
 * 与旧 `getWikiPage` / `getWikiContents`（openkb 只读投影）的关系：
 * - 旧接口按 `{section}/{stem}`（summaries|concepts|entities）寻址，服务旧组件；
 * - 本文件按**引擎 canonical 路径**（`canteen/lunch-dishes.md`）寻址，页面身份唯一，
 *   目录（LLM 起草/调整）与页面级 References 都挂在这套路径上。
 *
 * 后端返回 snake_case，这里统一映射为 camelCase 后再交给组件。
 */
import { getData, request } from './request';
import type {
  WikiBacklink,
  WikiBacklinkResult,
  WikiClaim,
  WikiClaimResult,
  WikiLinkMeta,
  WikiOutline,
  WikiOutlineNode,
  WikiOutlineView,
  WikiPageReference,
  WikiPageContent,
  WikiReaderPage,
  WikiTopicEdge,
} from '@/types/domainKnowledge';

// ── 后端原始结构（snake_case，不出本文件）──

interface BackendOutlineEntry {
  path: string;
  title: string;
  role: string;
  category: string;
  note: string;
  order: number;
}

interface BackendOutlineNode {
  id: string;
  kind: string;
  title: string;
  description: string;
  order: number;
  entries: BackendOutlineEntry[];
  children: BackendOutlineNode[];
}

interface BackendOutline {
  version: number;
  overview: string;
  topics: BackendOutlineNode[];
  page_count: number;
  covered_count: number;
}

interface BackendReaderPage {
  path: string;
  title: string;
  type: string;
  category: string;
  description: string;
  aliases: string[];
  tags: string[];
  why_visit: string;
  reference_count: number;
  fields?: string[];
  scope?: Record<string, unknown>;
}

interface BackendTopicEdge {
  source: string;
  target: string;
  weight: number;
  reasons?: string[];
}

interface BackendOutlineView {
  knowledge_base_id: string;
  page_count: number;
  pages: BackendReaderPage[];
  outline: BackendOutline | null;
  topic_edges?: BackendTopicEdge[];
  outline_version: number;
  source: string | null;
  model: string | null;
  instruction: string | null;
  generated_at: string | null;
  catalog_fingerprint: string;
  stale: boolean;
}

interface BackendReference {
  document_id: string;
  file_name?: string;
  mime_type?: string;
  excerpts?: string[];
  claim_count?: number;
  start_char?: number | null;
  end_char?: number | null;
}

interface BackendPageContent extends Partial<Omit<WikiPageContent, 'references' | 'linkMeta'>> {
  /** 适配器输出的 canonical 路径（与 path 一致；显式映射，避免 camelCase 漏转） */
  engine_path?: string;
  table_data?: WikiPageContent['tableData'];
  images_total?: number;
  bundle_sha?: string;
  image_error?: string;
  why_visit?: string;
  references?: BackendReference[];
  link_meta?: { path: string; kind?: string; why_visit?: string }[];
}

// ── snake → camel ──

function mapNodes(nodes: BackendOutlineNode[] | undefined): WikiOutlineNode[] {
  return (nodes || []).map((node) => ({
    id: node.id,
    kind: (node.kind as WikiOutlineNode['kind']) || 'section',
    title: node.title,
    description: node.description || '',
    order: node.order || 0,
    entries: (node.entries || []).map((e) => ({
      path: e.path,
      title: e.title,
      role: e.role || 'concept',
      category: e.category || '',
      note: e.note || '',
      order: e.order || 0,
    })),
    children: mapNodes(node.children),
  }));
}

function mapOutline(outline: BackendOutline | null): WikiOutline | null {
  if (!outline) return null;
  return {
    version: outline.version || 1,
    overview: outline.overview || '',
    topics: mapNodes(outline.topics),
    pageCount: outline.page_count || 0,
    coveredCount: outline.covered_count || 0,
  };
}

function mapPages(pages: BackendReaderPage[] | undefined): WikiReaderPage[] {
  return (pages || []).map((p) => ({
    path: p.path,
    title: p.title,
    type: p.type || 'concept',
    category: p.category || '',
    description: p.description || '',
    aliases: p.aliases || [],
    tags: p.tags || [],
    whyVisit: p.why_visit || '',
    referenceCount: p.reference_count || 0,
    fields: p.fields || [],
    scope: p.scope || {},
  }));
}

function mapTopicEdges(edges: BackendTopicEdge[] | undefined): WikiTopicEdge[] {
  return (edges || []).map((e) => ({
    source: e.source,
    target: e.target,
    weight: e.weight || 1,
    reasons: e.reasons || [],
  }));
}

function mapReferences(refs: BackendReference[] | undefined): WikiPageReference[] {
  return (refs || []).map((r) => ({
    documentId: r.document_id,
    fileName: r.file_name || '',
    mimeType: r.mime_type || '',
    excerpts: r.excerpts || [],
    claimCount: r.claim_count || 0,
    startChar: r.start_char ?? null,
    endChar: r.end_char ?? null,
  }));
}

function mapView(res: BackendOutlineView): WikiOutlineView {
  return {
    knowledgeBaseId: res.knowledge_base_id,
    pageCount: res.page_count || 0,
    pages: mapPages(res.pages),
    outline: mapOutline(res.outline),
    outlineVersion: res.outline_version || 0,
    source: res.source || null,
    model: res.model || null,
    instruction: res.instruction || null,
    generatedAt: res.generated_at || null,
    catalogFingerprint: res.catalog_fingerprint || '',
    stale: res.stale === true,
    topicEdges: mapTopicEdges(res.topic_edges),
  };
}

// ── 接口 ──

/**
 * 读取目录 + 扁平页面清单（阅读页首屏一次拿全）。
 *
 * `documentId` 非空 = 文档级阅读视图（文档详情页的「Wiki 界面」）：页面清单按该文档
 * 过滤，落库的知识库级目录做过滤投影（目录本身仍是知识库级的，不会被改写）。
 */
export function getWikiOutline(kbId: string, documentId?: string): Promise<WikiOutlineView> {
  return getData<BackendOutlineView>(
    request.get('/knowledge-base/wiki/outline', {
      params: {
        knowledge_base_id: kbId,
        ...(documentId ? { document_id: documentId } : {}),
      },
    }),
  ).then(mapView);
}

/** LLM 起草目录（覆盖旧 active 版本）。 */
export function draftWikiOutline(
  kbId: string,
  options?: { extraInstruction?: string; unclassifiedTitle?: string; model?: string },
): Promise<WikiOutlineView> {
  return getData<BackendOutlineView>(
    request.post('/knowledge-base/wiki/outline/draft', {
      knowledge_base_id: kbId,
      ...(options?.extraInstruction ? { extra_instruction: options.extraInstruction } : {}),
      ...(options?.unclassifiedTitle ? { unclassified_title: options.unclassifiedTitle } : {}),
      ...(options?.model ? { model: options.model } : {}),
    }),
  ).then(mapView);
}

/** LLM 按自然语言要求调整目录（未提及部分保持原样）。 */
export function adjustWikiOutline(
  kbId: string,
  instruction: string,
  options?: { expectedVersion?: number; unclassifiedTitle?: string; model?: string },
): Promise<WikiOutlineView> {
  return getData<BackendOutlineView>(
    request.post('/knowledge-base/wiki/outline/adjust', {
      knowledge_base_id: kbId,
      instruction,
      ...(options?.expectedVersion != null ? { expected_version: options.expectedVersion } : {}),
      ...(options?.unclassifiedTitle ? { unclassified_title: options.unclassifiedTitle } : {}),
      ...(options?.model ? { model: options.model } : {}),
    }),
  ).then(mapView);
}

/** 读取单个页面（canonical 路径）+ 页面级 References。 */
export function getWikiReaderPage(kbId: string, path: string): Promise<WikiPageContent> {
  return getData<BackendPageContent>(
    request.get('/knowledge-base/parse-results/wiki-page', {
      params: { knowledge_base_id: kbId, path },
    }),
  ).then((res) => ({
    ...(res as WikiPageContent),
    // snake_case 字段显式映射（适配器返回的是引擎原始命名）
    enginePath: res.engine_path || res.path,
    whyVisit: res.why_visit || '',
    aliases: res.aliases || [],
    tags: res.tags || [],
    limitations: res.limitations || [],
    links: res.links || [],
    related: res.related || [],
    fields: res.fields || [],
    scope: res.scope || {},
    linkMeta: (res.link_meta || []).map((l): WikiLinkMeta => ({
      path: l.path,
      kind: l.kind || '',
      whyVisit: l.why_visit || '',
    })),
    references: mapReferences(res.references),
    tableData: res.table_data,
    images: res.images || [],
    imagesTotal: res.images_total || 0,
    bundleSha: res.bundle_sha,
    imageError: res.image_error,
  }));
}

/** 「链入本页」：谁链接到了这一页（frontmatter 页间链接 + 正文首次提及）。 */
export function getWikiBacklinks(kbId: string, path: string, documentId?: string): Promise<WikiBacklinkResult> {
  return getData<{
    path: string;
    total: number;
    mention_count: number;
    items: { path: string; title: string; kind: string }[];
  }>(
    request.get('/knowledge-base/wiki/backlinks', {
      params: {
        knowledge_base_id: kbId,
        path,
        ...(documentId ? { document_id: documentId } : {}),
      },
    }),
  ).then((res) => ({
    path: res.path || path,
    total: res.total || 0,
    mentionCount: res.mention_count || 0,
    items: (res.items || []).map((i): WikiBacklink => ({
      path: i.path,
      title: i.title || i.path,
      kind: i.kind || 'link',
    })),
  }));
}

/** claim 级溯源明细（列式视图·溯源审计）：按页面或按来源文档过滤。 */
export function getWikiClaims(
  kbId: string,
  options: { path?: string; documentId?: string; limit?: number },
): Promise<WikiClaimResult> {
  return getData<{
    total: number;
    items: {
      claim_id: string;
      page_path: string;
      page_title?: string;
      text: string;
      excerpt: string;
      chunk_id: string;
      start_char: number | null;
      end_char: number | null;
      support_status: string;
      documents?: { document_id: string; file_name?: string; mime_type?: string }[];
    }[];
  }>(
    request.get('/knowledge-base/wiki/claims', {
      params: {
        knowledge_base_id: kbId,
        ...(options.path ? { path: options.path } : {}),
        ...(options.documentId ? { document_id: options.documentId } : {}),
        ...(options.limit ? { limit: options.limit } : {}),
      },
    }),
  ).then((res) => ({
    total: res.total || 0,
    items: (res.items || []).map((c): WikiClaim => ({
      claimId: c.claim_id,
      pagePath: c.page_path,
      pageTitle: c.page_title || c.page_path,
      text: c.text,
      excerpt: c.excerpt,
      chunkId: c.chunk_id,
      startChar: c.start_char ?? null,
      endChar: c.end_char ?? null,
      supportStatus: c.support_status,
      documents: (c.documents || []).map((d) => ({
        documentId: d.document_id,
        fileName: d.file_name || '',
        mimeType: d.mime_type || '',
      })),
    })),
  }));
}
