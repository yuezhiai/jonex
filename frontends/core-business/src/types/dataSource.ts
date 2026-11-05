// `database` 是**结构来源**而不是文档来源：它只读 catalog 出 schema、不取文档，
// 所以它走的链路（生成 schema、编辑 Schema）与其他类型完全不同。
export type AccessType = 'api' | 'api_push' | 'storage' | 'file' | 'mqtt' | 'database' | 'etl';

/** 接入目录项（来自 /ecosystem/data-access-methods） */
export interface AccessMethodItem {
  id: string;
  name: string;
  accessType: AccessType;
  description?: string;
  status: string;
}

/** 数据源实例（后端 snake → 前端 camel） */
export interface DataSourceInstance {
  id: string;
  knowledgeBaseId: string;
  /** 非空 = 本行引用的是平台连接池里的连接（配置从那边读）；空 = 存量内联数据源 */
  connectionId?: string;
  accessMethodId?: string;
  accessType: AccessType;
  name: string;
  configJson: Record<string, any>;
  syncMode: 'manual' | 'scheduled';
  status: 'active' | 'paused' | 'error';
  lastSyncAt?: string | null;
  lastSyncStatus?: string | null;
  lastSyncMessage?: string | null;
  documentCount: number;
  createdAt?: string | null;
  updatedAt?: string | null;
  // 仅创建 api_push / 重置 key 时一次性返回
  ingestKey?: string;
  ingestUrl?: string;
}

export interface CreateDataSourcePayload {
  knowledge_base_id: string;
  access_method_id?: string;
  access_type: AccessType;
  name: string;
  config_json: Record<string, any>;
  sync_mode?: 'manual' | 'scheduled';
  /** 引用平台连接池里的连接。传了它就不传 config_json 的连接字段（后端会忽略） */
  connection_id?: string;
}

export interface TestResult {
  ok: boolean;
  message: string;
  sample_count?: number;
}

export interface SyncResult {
  created: number;
  failed: number;
  message?: string | null;
}

export interface DataSourceDoc {
  id: string;
  name: string;
  type: string;
  size: string;
  uploadTime: string;
  status: string;
}

/** 平台级连接池里的一条连接（后端 `DataSourceConnection.to_dict()`）。 */
export interface ConnectionItem {
  id: string;
  name: string;
  access_type: string;
  /** `password_ref` 已被后端脱敏成 `***` */
  config_json: Record<string, any>;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}
