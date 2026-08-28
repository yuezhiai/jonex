import apiClient from './request';

export interface AdapterListResponse {
  items: AdapterItem[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface AdapterItem {
  id: string;
  tenant_id: string;
  name: string;
  adapter_type: string;
  config_json: Record<string, unknown>;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface SaveAdapterPayload {
  name: string;
  adapter_type: string;
  config_json?: Record<string, unknown>;
}

const ADAPTER_TYPE_LABELS: Record<string, string> = {
  dingtalk: 'ecosystem.adapterTypeDingtalk',
  wechat_work: 'ecosystem.adapterTypeWechatWork',
  feishu: 'ecosystem.adapterTypeFeishu',
};

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  connected: { label: 'ecosystem.adapterStatusConnected', color: 'green' },
  disconnected: { label: 'ecosystem.adapterStatusDisconnected', color: 'default' },
  error: { label: 'ecosystem.adapterStatusError', color: 'red' },
};

export function getAdapterTypeLabel(type: string, t: (key: string) => string): string {
  return ADAPTER_TYPE_LABELS[type] ? t(ADAPTER_TYPE_LABELS[type]) : type;
}

export function getAdapterStatusLabel(status: string, t: (key: string) => string): { label: string; color: string } {
  const entry = STATUS_LABELS[status];
  return entry ? { label: t(entry.label), color: entry.color } : { label: status, color: 'default' };
}

export const ADAPTER_TYPE_OPTIONS: string[] = ['dingtalk', 'wechat_work', 'feishu'];

export async function listAdapters(offset = 0, limit = 100): Promise<AdapterListResponse> {
  return apiClient.get<AdapterListResponse>('/ecosystem/adapters', {
    params: { offset, limit },
  });
}

export async function createAdapter(data: SaveAdapterPayload): Promise<AdapterItem> {
  return apiClient.post<AdapterItem>('/ecosystem/adapters', data);
}

export async function updateAdapter(
  id: string,
  data: Partial<SaveAdapterPayload & { status: string }>,
): Promise<AdapterItem> {
  return apiClient.patch<AdapterItem>(`/ecosystem/adapters/${id}`, data);
}

export async function connectAdapter(id: string): Promise<AdapterItem> {
  return apiClient.post<AdapterItem>(`/ecosystem/adapters/${id}/connect`);
}

export async function disconnectAdapter(id: string): Promise<AdapterItem> {
  return apiClient.post<AdapterItem>(`/ecosystem/adapters/${id}/disconnect`);
}
