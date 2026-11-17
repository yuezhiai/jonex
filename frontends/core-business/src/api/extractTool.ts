import type { ExtractionSkillRef } from '@/types/domainKnowledge';
import { postData } from './request';

export type ExtractToolGroup = 'refine' | 'constrain';

/** 试跑结果状态：executed / failed 是真实调用过；其余都没调用。 */
export type ExtractToolPreviewStatus =
  | 'executed'
  | 'failed'
  | 'no_entity'
  | 'missing_inputs'
  | 'tool_unavailable'
  | 'not_read_only'
  | 'no_runtime'
  | string;

export interface ExtractToolPreviewResult {
  status: ExtractToolPreviewStatus;
  group: ExtractToolGroup;
  /** true = 只探测输出，writes 为空，不代表最终声明 */
  probe_only?: boolean;
  /** 图里最近更新的同类型实体名（最多 20 个），用来换一个实体再试 */
  candidates: string[];
  entity: { canonical_name: string; entity_type: string; attributes: Record<string, unknown> } | null;
  inputs: Record<string, unknown> | null;
  tool: { source: 'kb' | 'platform'; tool_name: string } | null;
  output?: unknown;
  error?: string | null;
  /** refine：本次会写入的属性 */
  writes?: Record<string, unknown>;
  /** refine：调用成功但输出里取不到值的写入项（多半是输出路径写错了） */
  unresolved_writes?: string[];
  /** refine：调用失败且 on_error=drop_attr 时会被清掉的属性 */
  dropped?: string[];
  /** constrain：是否会剔除该实体 */
  rejected?: boolean;
}

export interface ExtractToolAiPreviewSummary {
  status?: string;
  unresolved_writes?: string[];
  writes?: Record<string, unknown>;
  output_paths?: { path: string; type: string; sample: string }[];
}

export interface ExtractToolAiProposal {
  assistSessionId: string;
  status:
    | 'ready'
    | 'invalid'
    | 'disabled'
    | 'tool_unavailable'
    | 'tool_not_read_only'
    | 'failed';
  group: ExtractToolGroup;
  round: number;
  contextHash: string | null;
  declaration: ExtractionSkillRef | null;
  warnings: string[];
  error: string | null;
}

interface BackendExtractToolAiProposal {
  assist_session_id?: string;
  status?: ExtractToolAiProposal['status'];
  group?: ExtractToolGroup;
  round?: number;
  context_hash?: string | null;
  declaration?: ExtractionSkillRef | null;
  warnings?: string[];
  error?: string | null;
}

function mapExtractToolAiProposal(p: BackendExtractToolAiProposal): ExtractToolAiProposal {
  return {
    assistSessionId: p.assist_session_id || '',
    status: p.status || 'failed',
    group: p.group === 'constrain' ? 'constrain' : 'refine',
    round: p.round || 1,
    contextHash: p.context_hash ?? null,
    declaration: p.declaration || null,
    warnings: p.warnings || [],
    error: p.error ?? null,
  };
}

export interface ProposeExtractToolDeclarationInput {
  entityType?: string;
  group: ExtractToolGroup;
  toolSource: 'kb' | 'platform';
  toolName: string;
  attributes: string[];
  currentDeclaration?: ExtractionSkillRef | null;
}

export async function proposeExtractToolDeclaration(
  kbId: string,
  input: ProposeExtractToolDeclarationInput,
): Promise<ExtractToolAiProposal> {
  const res = await postData<BackendExtractToolAiProposal>(
    '/knowledge-base/ontology/extract-tool-declarations/recommend',
    {
      knowledge_base_id: kbId,
      entity_type: input.entityType || '',
      group: input.group,
      tool_source: input.toolSource,
      tool_name: input.toolName,
      attributes: input.attributes,
      current_declaration: input.currentDeclaration || {},
    },
    { timeout: 60000 },
  );
  return mapExtractToolAiProposal(res);
}

export interface RefineExtractToolDeclarationInput extends ProposeExtractToolDeclarationInput {
  assistSessionId: string;
  round: number;
  contextHash: string | null;
  preview?: ExtractToolAiPreviewSummary;
}

export async function refineExtractToolDeclaration(
  kbId: string,
  input: RefineExtractToolDeclarationInput,
): Promise<ExtractToolAiProposal> {
  const res = await postData<BackendExtractToolAiProposal>(
    '/knowledge-base/ontology/extract-tool-declarations/refine',
    {
      knowledge_base_id: kbId,
      entity_type: input.entityType || '',
      group: input.group,
      tool_source: input.toolSource,
      tool_name: input.toolName,
      attributes: input.attributes,
      current_declaration: input.currentDeclaration || {},
      assist_session_id: input.assistSessionId,
      round: input.round,
      context_hash: input.contextHash,
      preview: input.preview || {},
    },
    { timeout: 60000 },
  );
  return mapExtractToolAiProposal(res);
}

/** 按（可以还没保存的）声明，拿图里一个现有实体真实调用一次工具；不写图，记一条审计。 */
export function previewExtractTool(
  kbId: string,
  input: {
    entityType: string;
    group: ExtractToolGroup;
    declaration: ExtractionSkillRef;
    canonicalName?: string;
    /** 只探测工具输出：允许 writes 为空，后端用临时写入口过校验 */
    probeOnly?: boolean;
  },
): Promise<ExtractToolPreviewResult> {
  return postData<ExtractToolPreviewResult>(
    '/knowledge-base/ontology/extract-tools/preview',
    {
      knowledge_base_id: kbId,
      entity_type: input.entityType,
      group: input.group,
      declaration: input.declaration,
      ...(input.canonicalName ? { canonical_name: input.canonicalName } : {}),
      ...(input.probeOnly ? { probe_only: true } : {}),
    },
    { timeout: 30000 },
  );
}
