import { request, getData } from './request';
import type {
  DomainServiceItem,
  DomainServiceListResult,
  DomainServiceFormData,
  ServiceApiKeyItem,
} from '../types/domainService';

/** 获取领域服务列表 */
export async function listServices(space_id?: string, offset = 0, limit = 100): Promise<DomainServiceListResult> {
  const params: Record<string, string | number> = { offset, limit };
  if (space_id) params.space_id = space_id;
  return getData<DomainServiceListResult>(request.get('/knowledge-base/services', { params }));
}

/** 创建领域服务 */
export async function createService(data: DomainServiceFormData): Promise<DomainServiceItem> {
  return getData<DomainServiceItem>(request.post('/knowledge-base/services', data));
}

/** 获取领域服务详情 */
export async function getService(serviceId: string): Promise<DomainServiceItem> {
  return getData<DomainServiceItem>(request.get(`/knowledge-base/services/${serviceId}`));
}

/** 更新领域服务 */
export async function updateService(
  serviceId: string,
  data: Partial<DomainServiceFormData>,
): Promise<DomainServiceItem> {
  return getData<DomainServiceItem>(request.patch(`/knowledge-base/services/${serviceId}`, data));
}

/** 删除领域服务 */
export async function deleteService(serviceId: string): Promise<boolean> {
  await getData(request.delete(`/knowledge-base/services/${serviceId}`));
  return true;
}

/** 获取领域服务 API Key 列表 */
export async function listServiceApiKeys(serviceId: string): Promise<{ items: ServiceApiKeyItem[]; total: number }> {
  return getData(request.get(`/knowledge-base/services/${serviceId}/api-keys`));
}

/** 创建 API Key */
export async function createServiceApiKey(
  serviceId: string,
  data?: { key_prefix?: string; expires_in_days?: number },
): Promise<ServiceApiKeyItem> {
  return getData(request.post(`/knowledge-base/services/${serviceId}/api-keys`, data || {}));
}

/** 删除 API Key */
export async function deleteServiceApiKey(serviceId: string, keyId: string): Promise<boolean> {
  return getData(request.delete(`/knowledge-base/services/${serviceId}/api-keys/${keyId}`));
}

/** 轮换 API Key */
export async function rotateApiKey(serviceId: string): Promise<{ api_key: string }> {
  return getData(request.post(`/knowledge-base/services/${serviceId}/rotate-api-key`));
}

