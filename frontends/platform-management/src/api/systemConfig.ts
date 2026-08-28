import apiClient from './request';

export interface SystemConfigItem {
  id: number;
  config_group: string;
  config_key: string;
  config_value: string | null;
  value_type: string;
  description: string | null;
}

export async function listSystemConfigs(): Promise<{ items: SystemConfigItem[] }> {
  return apiClient.get<{ items: SystemConfigItem[] }>('/platform/system-configs');
}

export async function updateSystemConfig(key: string, value: string): Promise<SystemConfigItem> {
  return apiClient.put<SystemConfigItem>(`/platform/system-configs/${key}`, {
    config_value: value,
  });
}
