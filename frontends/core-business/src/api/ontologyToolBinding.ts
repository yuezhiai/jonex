import type {
  BindableTool,
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
  read_only?: boolean;
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
