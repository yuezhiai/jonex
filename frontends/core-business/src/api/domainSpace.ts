import { request, getData } from './request';
import type { DomainSpace, DomainSpaceFormData, DomainSpaceListResult, SpacePermission } from '../types/domainSpace';

/** 获取领域空间列表 */
export async function listSpaces(offset = 0, limit = 50): Promise<DomainSpaceListResult> {
  return getData<DomainSpaceListResult>(request.get('/knowledge-base/spaces', { params: { offset, limit } }));
}

/** 获取领域空间详情 */
export async function getSpace(spaceId: string): Promise<DomainSpace> {
  return getData<DomainSpace>(request.get(`/knowledge-base/spaces/${spaceId}`));
}

/** 创建领域空间 */
export async function createSpace(data: DomainSpaceFormData): Promise<DomainSpace> {
  return getData<DomainSpace>(request.post('/knowledge-base/spaces', data));
}

/** 更新领域空间 */
export async function updateSpace(spaceId: string, data: Partial<DomainSpaceFormData>): Promise<DomainSpace> {
  return getData<DomainSpace>(request.patch(`/knowledge-base/spaces/${spaceId}`, data));
}

/** 删除领域空间 */
export async function deleteSpace(spaceId: string): Promise<void> {
  await getData(request.delete(`/knowledge-base/spaces/${spaceId}`));
}

/** 获取空间权限列表 */
export interface SpaceOwnerInfo {
  user_id: string;
  display_name: string | null;
}

export async function getSpacePermissions(
  spaceId: string,
): Promise<{ permissions: SpacePermission[]; owner: SpaceOwnerInfo | null }> {
  const result = await getData<{ permissions: SpacePermission[]; owner: SpaceOwnerInfo | null }>(
    request.get(`/knowledge-base/spaces/${spaceId}/permissions`),
  );
  return { permissions: result.permissions ?? [], owner: result.owner ?? null };
}

/** 更新空间权限 */
export async function updateSpacePermissions(
  spaceId: string,
  permissions: { user_id: string; role: string }[],
): Promise<void> {
  await getData(request.put(`/knowledge-base/spaces/${spaceId}/permissions`, { permissions }));
}
