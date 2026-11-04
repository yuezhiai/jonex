import { apiClient } from './auth';

export interface ShellSpaceItem {
  id: string;
  name: string;
  /**
   * 领域空间状态。`active` 之外一律视为「已禁用」：
   * 管理员仍能看到该条目（用于恢复），但不可选中；普通成员的列表里不会有它。
   */
  status?: string;
}

export async function fetchSpaces(): Promise<ShellSpaceItem[]> {
  const data = await apiClient.get<{ items: ShellSpaceItem[]; total: number }>('/knowledge-base/spaces', {
    params: { limit: 100 },
  });
  return data?.items || [];
}
