import apiClient from './request';

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

// ══ 接口方法（apiClient.get<T> 已解包，返回 data）══

/** 获取当前租户下全部领域空间列表（limit 100 取满） */
export async function listSpaces(): Promise<SpaceItem[]> {
  const data = await apiClient.get<{ items: SpaceItem[]; total: number }>('/knowledge-base/spaces', {
    params: { offset: 0, limit: 100 },
  });
  return data?.items ?? [];
}

/** 获取知识库列表，可按领域空间过滤；spaceId 为空返回全量 */
export async function listKnowledgeBases(spaceId?: string): Promise<KnowledgeBaseBrief[]> {
  const data = await apiClient.get<{ items: KnowledgeBaseBrief[]; total: number }>('/knowledge-base/knowledge-info', {
    params: { space_id: spaceId || undefined, offset: 0, limit: 100 },
  });
  return data?.items ?? [];
}

/** 获取指定知识库下所有目录（平铺 folder_id 列表，无 parent_id） */
export async function listKbFolders(kbId: string): Promise<KbFolderItem[]> {
  const data = await apiClient.get<{ items: KbFolderItem[]; total: number }>('/knowledge-base/folders', {
    params: { knowledge_base_id: kbId },
  });
  return data?.items ?? [];
}
