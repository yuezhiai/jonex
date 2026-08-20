import apiClient from './client';

// ══ 领域空间 / 知识库列表类型（来源 /api/v1/knowledge-base/*）══

/** 领域空间简要信息（listSpaces 返回项） */
export interface SpaceItem {
  id: string;
  name: string;
  description?: string | null;
}

/** 知识库简要信息（listKnowledgeBases 返回项） */
export interface KnowledgeBaseBrief {
  id: string;
  name: string;
  space_id?: string | null;
}

/** 知识库目录简要信息（listKbFolders 返回项，来源 knowledge_base.folders） */
export interface KbFolderItem {
  id: string;
  name: string;
  knowledge_base_id: string;
  is_preset?: boolean;
}

// ══ 统一解包：兼容 { success } 与 { code } 两种响应信封 ══

interface ApiEnvelope<T> {
  success?: boolean;
  code?: number;
  message?: string;
  data?: T;
}

function unwrap<T>(payload: ApiEnvelope<T>): T {
  if (payload?.success === false || (typeof payload?.code === 'number' && payload.code !== 0)) {
    throw new Error(payload.message || 'Request failed');
  }
  return payload.data as T;
}

// ══ 接口方法 ══

/** 获取当前租户下全部领域空间列表（limit 100 取满） */
export async function listSpaces(): Promise<SpaceItem[]> {
  const resp = await apiClient.get<ApiEnvelope<{ items: SpaceItem[]; total: number }>>(
    '/api/v1/knowledge-base/spaces',
    { params: { offset: 0, limit: 100 } },
  );
  const data = unwrap(resp.data);
  return data?.items ?? [];
}

/** 获取知识库列表，可按领域空间过滤；spaceId 为空返回全量 */
export async function listKnowledgeBases(spaceId?: string): Promise<KnowledgeBaseBrief[]> {
  const resp = await apiClient.get<ApiEnvelope<{ items: KnowledgeBaseBrief[]; total: number }>>(
    '/api/v1/knowledge-base/knowledge-info',
    { params: { space_id: spaceId || undefined, offset: 0, limit: 100 } },
  );
  const data = unwrap(resp.data);
  return data?.items ?? [];
}

/** 获取指定知识库下所有目录（平铺 folder_id 列表，无 parent_id） */
export async function listKbFolders(kbId: string): Promise<KbFolderItem[]> {
  const resp = await apiClient.get<ApiEnvelope<{ items: KbFolderItem[]; total: number }>>(
    '/api/v1/knowledge-base/folders',
    { params: { knowledge_base_id: kbId } },
  );
  const data = unwrap(resp.data);
  return data?.items ?? [];
}
