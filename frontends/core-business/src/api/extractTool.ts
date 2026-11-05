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

/** 按（可以还没保存的）声明，拿图里一个现有实体真实调用一次工具；不写图，记一条审计。 */
export function previewExtractTool(
  kbId: string,
  input: {
    entityType: string;
    group: ExtractToolGroup;
    declaration: ExtractionSkillRef;
    canonicalName?: string;
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
    },
    { timeout: 30000 },
  );
}
