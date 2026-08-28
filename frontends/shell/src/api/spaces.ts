import { apiClient } from './auth';

export interface ShellSpaceItem {
  id: string;
  name: string;
}

export async function fetchSpaces(): Promise<ShellSpaceItem[]> {
  const data = await apiClient.get<{ items: ShellSpaceItem[]; total: number }>('/knowledge-base/spaces', {
    params: { limit: 100 },
  });
  return data?.items || [];
}
