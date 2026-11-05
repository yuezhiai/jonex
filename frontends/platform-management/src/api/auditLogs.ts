import apiClient from './request';

export interface AuditLogItem {
  id: number;
  tenant_id: string;
  user_id: number | null;
  username: string | null;
  ip: string | null;
  action: string;
  resource: string | null;
  resource_label: string | null;
  resource_id: string | null;
  resource_name: string | null;
  status_code: number | null;
  duration_ms: number | null;
  detail: string | null;
  trace_id: string | null;
  created_at: string | null;
  /** 执行结果：SUCCESS / FAILED（详情接口返回） */
  outcome?: string | null;
  /** 失败原因（详情接口返回） */
  error_message?: string | null;
  /** 错误堆栈（详情接口返回） */
  error_stack?: string | null;
  request_params?: unknown;
  response_body?: unknown;
  log_level?: string | null;
  /** 日志类型：API / 任务 / 登录等来源分类（详情接口返回） */
  log_type?: string | null;
  /** 产生日志的服务名（详情接口返回） */
  service_name?: string | null;
  method?: string | null;
  path?: string | null;
}

export interface AuditLogListResponse {
  total: number;
  items: AuditLogItem[];
}

export interface AuditActionOption {
  action: string;
  label_zh: string;
  label_en: string;
}

export async function listAuditLogs(
  params: {
    page?: number;
    page_size?: number;
    user_id?: number;
    action?: string;
    /** 执行结果筛选：SUCCESS / FAILED（后端 query 已支持） */
    outcome?: string;
    /** 日志类型筛选（预留：bridge.log 等投递日志用） */
    log_type?: string;
    resource?: string;
    keyword?: string;
    start_time?: string;
    end_time?: string;
  } = {},
): Promise<AuditLogListResponse> {
  return apiClient.get<AuditLogListResponse>('/platform/audit-logs', { params });
}

export async function getAuditLog(id: number): Promise<AuditLogItem> {
  return apiClient.get<AuditLogItem>(`/platform/audit-logs/${id}`);
}

export async function listAuditActions(): Promise<AuditActionOption[]> {
  const data = await apiClient.get<{ actions: AuditActionOption[] }>('/platform/audit-logs/actions');
  return data?.actions ?? [];
}

export interface AuditResourceType {
  resource: string;
  label_zh: string;
  label_en: string;
}

export async function listAuditResourceTypes(): Promise<AuditResourceType[]> {
  const data = await apiClient.get<{ resources: AuditResourceType[] }>('/platform/audit-logs/resource-types');
  return data?.resources ?? [];
}

/** 根据当前 locale 获取操作类型显示名 */
export function getActionLabelByLocale(action: string, labelZh: string, labelEn: string, locale: string): string {
  return locale?.startsWith('zh') ? labelZh : labelEn;
}
