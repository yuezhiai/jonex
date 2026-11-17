import type {
  BindableTool,
  BindingMappingProposal,
  BindingMappingQuestion,
  BindingMappingSuggestion,
  MappingPreview,
  ToolBinding,
  ToolBindingEntityType,
  ToolBindingListResult,
  ToolSource,
} from '@/types/domainKnowledge';
import { request, getData, postData } from './request';

// 后端返回结构（snake_case）
interface BackendToolBinding {
  id: string;
  knowledge_base_id: string;
  entity_type: ToolBindingEntityType;
  canonical_name: string;
  tool_source?: ToolSource;
  tool_name: string;
  tool_label?: string | null;
  param_mapping?: Record<string, unknown> | null;
  enabled: boolean;
  qa_callable?: boolean;
  executable?: boolean;
  tool_available?: boolean;
  tool_read_only?: boolean;
  target_exists?: boolean | null;
  caller_supplied?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}
interface BackendToolBindingList {
  items: BackendToolBinding[];
  total: number;
  page: number;
  page_size: number;
}
interface BackendBindableTool {
  source?: ToolSource;
  tool_name: string;
  name: string;
  description?: string | null;
  input_schema?: Record<string, any> | null;
  output_schema?: Record<string, any> | null;
  read_only?: boolean;
}

interface BackendMappingPreview {
  status: MappingPreview['status'];
  status_message?: string | null;
  action?: {
    name?: string;
    found?: boolean;
    attributes?: { name: string; has_value?: boolean }[];
  };
  tool?: {
    source?: ToolSource;
    tool_name?: string;
    name?: string | null;
    read_only?: boolean;
    available?: boolean;
    input_schema?: Record<string, any>;
  };
  mapping?: Record<string, unknown>;
  parameters?: {
    resolved?: Record<string, unknown>;
    input_refs?: Record<string, string>;
    unresolved_attrs?: string[];
  };
  required?: string[];
  caller_supplied?: string[];
  empty_attr?: string[];
  unknown_keys?: string[];
  input_schema_declared?: boolean;
  warnings?: string[];
  errors?: string[];
}

function mapMappingPreview(p: BackendMappingPreview): MappingPreview {
  return {
    status: p.status,
    statusMessage: p.status_message ?? null,
    action: {
      name: p.action?.name || '',
      found: p.action?.found === true,
      attributes: (p.action?.attributes || []).map((a) => ({
        name: a.name,
        hasValue: a.has_value === true,
      })),
    },
    tool: {
      source: p.tool?.source || 'platform',
      toolName: p.tool?.tool_name || '',
      name: p.tool?.name ?? null,
      readOnly: p.tool?.read_only === true,
      available: p.tool?.available === true,
      inputSchema: p.tool?.input_schema || {},
    },
    mapping: p.mapping || {},
    parameters: {
      resolved: p.parameters?.resolved || {},
      inputRefs: p.parameters?.input_refs || {},
      unresolvedAttrs: p.parameters?.unresolved_attrs || [],
    },
    required: p.required || [],
    callerSupplied: p.caller_supplied || [],
    emptyAttr: p.empty_attr || [],
    unknownKeys: p.unknown_keys || [],
    inputSchemaDeclared: p.input_schema_declared === true,
    warnings: p.warnings || [],
    errors: p.errors || [],
  };
}

interface BackendBindingMappingSuggestion {
  param?: string;
  value?: unknown;
  provenance?: 'rule' | 'ai';
  confidence?: number;
  reason?: string;
}

interface BackendBindingMappingQuestion {
  id?: string;
  question?: string;
  options?: { label?: string; value?: string; recommended?: boolean }[];
  allow_free_text?: boolean;
}

interface BackendBindingMappingProposal {
  assist_session_id?: string;
  status?: BindingMappingProposal['status'];
  round?: number;
  context_hash?: string | null;
  mappings?: BackendBindingMappingSuggestion[];
  questions?: BackendBindingMappingQuestion[];
  warnings?: string[];
  error?: string | null;
}

function mapBindingMappingProposal(p: BackendBindingMappingProposal): BindingMappingProposal {
  return {
    assistSessionId: p.assist_session_id || '',
    status: p.status || 'failed',
    round: p.round || 1,
    contextHash: p.context_hash ?? null,
    mappings: (p.mappings || []).map((m): BindingMappingSuggestion => ({
      param: m.param || '',
      value: m.value,
      provenance: m.provenance === 'rule' ? 'rule' : 'ai',
      confidence: typeof m.confidence === 'number' ? m.confidence : 0,
      reason: m.reason || '',
    })).filter((m) => !!m.param),
    questions: (p.questions || []).map((q): BindingMappingQuestion => ({
      id: q.id || '',
      question: q.question || '',
      options: (q.options || []).map((o) => ({
        label: o.label || '',
        value: o.value || '',
        recommended: o.recommended === true,
      })),
      allowFreeText: q.allow_free_text !== false,
    })).filter((q) => !!q.question),
    warnings: p.warnings || [],
    error: p.error ?? null,
  };
}

function mapBinding(b: BackendToolBinding): ToolBinding {
  return {
    id: b.id,
    knowledgeBaseId: b.knowledge_base_id,
    entityType: b.entity_type,
    canonicalName: b.canonical_name,
    toolSource: b.tool_source || 'platform',
    toolName: b.tool_name,
    toolLabel: b.tool_label ?? null,
    paramMapping: b.param_mapping || {},
    enabled: !!b.enabled,
    qaCallable: b.qa_callable === true,
    // 旧后端不回 executable 时按类型推断：只有 Action 绑定参与执行
    executable: b.executable ?? b.entity_type === 'Action',
    toolAvailable: b.tool_available ?? false,
    toolReadOnly: b.tool_read_only === true,
    targetExists: b.target_exists === undefined ? null : b.target_exists,
    callerSupplied: b.caller_supplied || [],
    createdAt: b.created_at ?? null,
    updatedAt: b.updated_at ?? null,
  };
}

const BASE = '/knowledge-base/ontology/tool-bindings';

export async function listToolBindings(kbId: string, page = 1, pageSize = 20): Promise<ToolBindingListResult> {
  const res = await getData<BackendToolBindingList>(
    request.get(BASE, { params: { knowledge_base_id: kbId, page, page_size: pageSize } }),
  );
  return {
    items: (res.items || []).map(mapBinding),
    total: res.total ?? 0,
    page: res.page ?? page,
    pageSize: res.page_size ?? pageSize,
  };
}

export async function listBindableTools(kbId: string): Promise<BindableTool[]> {
  const res = await getData<{ items: BackendBindableTool[] }>(
    request.get(`${BASE}/tools`, { params: { knowledge_base_id: kbId } }),
  );
  return (res.items || []).map((t) => ({
    source: t.source || 'platform',
    toolName: t.tool_name,
    name: t.name,
    description: t.description || '',
    inputSchema: t.input_schema || {},
    outputSchema: t.output_schema || {},
    readOnly: t.read_only === true,
  }));
}

export interface CreateToolBindingInput {
  entityType: ToolBindingEntityType;
  canonicalName: string;
  toolSource: ToolSource;
  toolName: string;
  paramMapping?: Record<string, unknown>;
  enabled?: boolean;
  /** 允许知识库问答自动调用（默认 false） */
  qaCallable?: boolean;
}

export async function createToolBinding(kbId: string, input: CreateToolBindingInput): Promise<ToolBinding> {
  const res = await postData<BackendToolBinding>(BASE, {
    knowledge_base_id: kbId,
    entity_type: input.entityType,
    canonical_name: input.canonicalName,
    tool_source: input.toolSource,
    tool_name: input.toolName,
    param_mapping: input.paramMapping || {},
    enabled: input.enabled ?? true,
    qa_callable: input.qaCallable ?? false,
  });
  return mapBinding(res);
}

export interface UpdateToolBindingInput {
  /** 只在换工具时给（来源与工具名一起）；不给 = 工具不变，当前工具不可用时也能保存其它修改 */
  toolSource?: ToolSource;
  toolName?: string;
  paramMapping?: Record<string, unknown>;
  enabled?: boolean;
  qaCallable?: boolean;
}

/** 编辑绑定：实例（entity_type + canonical_name）不可改，其余按给了的字段更新。 */
export async function updateToolBinding(bindingId: string, input: UpdateToolBindingInput): Promise<ToolBinding> {
  const body: Record<string, unknown> = {};
  if (input.toolName !== undefined) {
    body.tool_name = input.toolName;
    body.tool_source = input.toolSource || 'platform';
  }
  if (input.paramMapping !== undefined) body.param_mapping = input.paramMapping;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  if (input.qaCallable !== undefined) body.qa_callable = input.qaCallable;
  const res = await getData<BackendToolBinding>(request.patch(`${BASE}/${bindingId}`, body));
  return mapBinding(res);
}

export async function setToolBindingEnabled(bindingId: string, enabled: boolean): Promise<ToolBinding> {
  const res = await getData<BackendToolBinding>(request.patch(`${BASE}/${bindingId}`, { enabled }));
  return mapBinding(res);
}

export async function setToolBindingQaCallable(bindingId: string, qaCallable: boolean): Promise<ToolBinding> {
  const res = await getData<BackendToolBinding>(
    request.patch(`${BASE}/${bindingId}`, { qa_callable: qaCallable }),
  );
  return mapBinding(res);
}

export async function deleteToolBinding(bindingId: string): Promise<void> {
  await getData(request.delete(`${BASE}/${bindingId}`));
}

export interface PreviewToolBindingMappingInput {
  canonicalName: string;
  toolSource: ToolSource;
  toolName: string;
  paramMapping: Record<string, unknown>;
}

/** 只读预览未保存的参数映射；不调用工具、不写库。 */
export async function previewToolBindingMapping(
  kbId: string,
  input: PreviewToolBindingMappingInput,
): Promise<MappingPreview> {
  const res = await postData<BackendMappingPreview>(`${BASE}/preview-mapping`, {
    knowledge_base_id: kbId,
    action_name: input.canonicalName,
    tool_source: input.toolSource,
    tool_name: input.toolName,
    param_mapping: input.paramMapping,
  });
  return mapMappingPreview(res);
}

export interface RecommendToolBindingMappingInput {
  canonicalName: string;
  toolSource: ToolSource;
  toolName: string;
  currentMapping: Record<string, unknown>;
}

/** AI 推荐未保存的参数映射；只返回建议，不写库、不调用工具。 */
export async function recommendToolBindingMapping(
  kbId: string,
  input: RecommendToolBindingMappingInput,
): Promise<BindingMappingProposal> {
  const res = await postData<BackendBindingMappingProposal>(`${BASE}/recommend-mapping`, {
    knowledge_base_id: kbId,
    action_name: input.canonicalName,
    tool_source: input.toolSource,
    tool_name: input.toolName,
    current_mapping: input.currentMapping,
  });
  return mapBindingMappingProposal(res);
}

export interface RefineToolBindingMappingInput extends RecommendToolBindingMappingInput {
  assistSessionId: string;
  answers: { question: string; answer: string }[];
  contextHash: string | null;
  round: number;
}

/** 带用户补充答案继续推荐；无状态，最多 3 轮。 */
export async function refineToolBindingMapping(
  kbId: string,
  input: RefineToolBindingMappingInput,
): Promise<BindingMappingProposal> {
  const res = await postData<BackendBindingMappingProposal>(`${BASE}/refine-mapping`, {
    knowledge_base_id: kbId,
    action_name: input.canonicalName,
    tool_source: input.toolSource,
    tool_name: input.toolName,
    current_mapping: input.currentMapping,
    assist_session_id: input.assistSessionId,
    answers: input.answers,
    context_hash: input.contextHash,
    round: input.round,
  });
  return mapBindingMappingProposal(res);
}
