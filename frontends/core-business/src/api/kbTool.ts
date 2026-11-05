// 知识库工具（KB 级工具目录）接口：本库登记的可执行 API 工具。
// 凭据只写不读：保存时传 secret，读取只回 has_secret。
import type { KbTool, KbToolRunResult } from '@/types/domainKnowledge';
import { request, getData, postData } from './request';

interface BackendKbTool {
  id: string;
  knowledge_base_id: string;
  tool_name: string;
  name: string;
  description?: string | null;
  input_schema?: Record<string, any> | null;
  output_schema?: Record<string, any> | null;
  runtime?: Record<string, any> | null;
  runtime_brief?: { type: 'echo' | 'api'; method?: string; url?: string; auth_type?: string } | null;
  has_secret?: boolean;
  enabled: boolean;
  read_only?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

interface BackendRunResult {
  status: KbToolRunResult['status'];
  ok: boolean;
  http_status?: number | null;
  output?: unknown;
  output_truncated?: boolean;
  error?: string | null;
  missing_inputs?: string[];
  duration_ms?: number;
}

function mapTool(t: BackendKbTool): KbTool {
  return {
    id: t.id,
    knowledgeBaseId: t.knowledge_base_id,
    toolName: t.tool_name,
    name: t.name,
    description: t.description || '',
    inputSchema: t.input_schema || {},
    outputSchema: t.output_schema || {},
    runtime: t.runtime ?? null,
    runtimeBrief: t.runtime_brief
      ? {
          type: t.runtime_brief.type,
          method: t.runtime_brief.method,
          url: t.runtime_brief.url,
          authType: t.runtime_brief.auth_type,
        }
      : null,
    hasSecret: !!t.has_secret,
    enabled: !!t.enabled,
    readOnly: t.read_only === true,
    createdAt: t.created_at ?? null,
    updatedAt: t.updated_at ?? null,
  };
}

const BASE = '/knowledge-base/ontology/kb-tools';

export async function listKbTools(kbId: string, keyword?: string): Promise<{ items: KbTool[]; total: number }> {
  const res = await getData<{ items: BackendKbTool[]; total: number }>(
    request.get(BASE, { params: { knowledge_base_id: kbId, page: 1, page_size: 200, keyword: keyword || undefined } }),
  );
  return { items: (res.items || []).map(mapTool), total: res.total ?? 0 };
}

/** 新建 / 编辑共用的提交体（snake_case，与后端一致） */
export interface SaveKbToolPayload {
  tool_name?: string;
  name?: string;
  description?: string | null;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  runtime?: Record<string, unknown> | null;
  secret?: string;
  clear_secret?: boolean;
  enabled?: boolean;
  /** 只读声明（显式勾选，默认 false = 写操作） */
  read_only?: boolean;
}

export async function createKbTool(kbId: string, payload: SaveKbToolPayload): Promise<KbTool> {
  const res = await postData<BackendKbTool>(BASE, { ...payload, knowledge_base_id: kbId });
  return mapTool(res);
}

export async function updateKbTool(toolId: string, payload: SaveKbToolPayload): Promise<KbTool> {
  const res = await getData<BackendKbTool>(request.patch(`${BASE}/${toolId}`, payload));
  return mapTool(res);
}

export async function deleteKbTool(toolId: string): Promise<void> {
  await getData(request.delete(`${BASE}/${toolId}`));
}

export async function testKbTool(toolId: string, args: Record<string, unknown>): Promise<KbToolRunResult> {
  const res = await postData<BackendRunResult>(`${BASE}/${toolId}/test`, { arguments: args });
  return {
    status: res.status,
    ok: !!res.ok,
    httpStatus: res.http_status ?? null,
    output: res.output,
    outputTruncated: !!res.output_truncated,
    error: res.error ?? null,
    missingInputs: res.missing_inputs || [],
    durationMs: res.duration_ms,
  };
}
