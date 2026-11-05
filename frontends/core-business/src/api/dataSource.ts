import { request, getData } from './request';
import type {
  AccessMethodItem,
  ConnectionItem,
  CreateDataSourcePayload,
  DataSourceInstance,
  SyncResult,
  TestResult,
} from '@/types/dataSource';
import type { ManualDocItem } from '@/types/domainKnowledge';
import { formatLocalDateTime } from '@/utils/dateTime';

interface BackendDataSource {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  connection_id?: string | null;
  access_method_id?: string | null;
  access_type: string;
  name: string;
  config_json: Record<string, any>;
  sync_mode: string;
  status: string;
  last_sync_at?: string | null;
  last_sync_status?: string | null;
  last_sync_message?: string | null;
  document_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  ingest_key?: string;
  ingest_url?: string;
}

function mapDataSource(b: BackendDataSource): DataSourceInstance {
  return {
    id: b.id,
    knowledgeBaseId: b.knowledge_base_id,
    connectionId: b.connection_id || undefined,
    accessMethodId: b.access_method_id || undefined,
    accessType: b.access_type as DataSourceInstance['accessType'],
    name: b.name,
    configJson: b.config_json || {},
    syncMode: (b.sync_mode as DataSourceInstance['syncMode']) || 'manual',
    status: (b.status as DataSourceInstance['status']) || 'active',
    lastSyncAt: b.last_sync_at,
    lastSyncStatus: b.last_sync_status,
    lastSyncMessage: b.last_sync_message,
    documentCount: b.document_count || 0,
    createdAt: b.created_at,
    updatedAt: b.updated_at,
    ingestKey: b.ingest_key,
    ingestUrl: b.ingest_url,
  };
}

// ── 接入目录（定义页数据，驱动"添加数据源"可选项）──
export async function listAccessMethods(): Promise<AccessMethodItem[]> {
  const res = await getData<{ items: any[] }>(
    request.get('/ecosystem/data-access-methods', { params: { offset: 0, limit: 100 } }),
  );
  return (res.items || [])
    .filter((i) => i.status === 'active')
    .map((i) => ({
      id: i.id,
      name: i.name,
      accessType: i.access_type,
      description: i.description,
      status: i.status,
    }));
}

// ── 数据源实例 CRUD ──
export async function listDataSources(kbId: string): Promise<DataSourceInstance[]> {
  const res = await getData<{ items: BackendDataSource[]; total: number }>(
    request.get('/knowledge-base/data-sources', { params: { knowledge_base_id: kbId } }),
  );
  return (res.items || []).map(mapDataSource);
}

export async function getDataSource(dsId: string): Promise<DataSourceInstance> {
  return mapDataSource(await getData<BackendDataSource>(request.get(`/knowledge-base/data-sources/${dsId}`)));
}

export async function createDataSource(payload: CreateDataSourcePayload): Promise<DataSourceInstance> {
  return mapDataSource(await getData<BackendDataSource>(request.post('/knowledge-base/data-sources', payload)));
}

export async function updateDataSource(
  dsId: string,
  payload: Partial<CreateDataSourcePayload> & { status?: string },
): Promise<DataSourceInstance> {
  return mapDataSource(
    await getData<BackendDataSource>(request.patch(`/knowledge-base/data-sources/${dsId}`, payload)),
  );
}

export async function deleteDataSource(dsId: string): Promise<void> {
  await getData(request.delete(`/knowledge-base/data-sources/${dsId}`));
}

export async function testDataSource(dsId: string): Promise<TestResult> {
  return getData<TestResult>(request.post(`/knowledge-base/data-sources/${dsId}/test`));
}

export async function syncDataSource(dsId: string): Promise<SyncResult> {
  return getData<SyncResult>(request.post(`/knowledge-base/data-sources/${dsId}/sync`));
}

export async function resetIngestKey(dsId: string): Promise<DataSourceInstance> {
  return mapDataSource(
    await getData<BackendDataSource>(request.post(`/knowledge-base/data-sources/${dsId}/reset-ingest-key`)),
  );
}

export interface LineageImportResult {
  imported: number
  skipped: number
  errors: string[]
  /** 全量错误计数（`errors` 只回前 20 条，计数不截断） */
  error_count: number
  total: number
}

export async function importLineageFile(dsId: string, file: File): Promise<LineageImportResult> {
  const form = new FormData()
  form.append('file', file)
  return getData<LineageImportResult>(
    request.post(`/knowledge-base/data-sources/${dsId}/lineage-import`, form),
  )
}

// ── 平台级连接池：知识库这边只**读**（选一个来引用），增删改在 platform-management ──
export async function listConnections(spaceId: string): Promise<ConnectionItem[]> {
  const res = await getData<{ items: ConnectionItem[] }>(
    request.get('/knowledge-base/data-source-connections', {
      params: { space_id: spaceId },
    }),
  );
  return res.items || [];
}

/**
 * 由该数据源的库表结构生成**这个知识库自己那份** schema（别的库不受影响）。
 *
 * 生成失败（`SCHEMA_GEN_BACKEND=remote` 时 `ONTO_INTEL_BASE` 不可达也走这里）
 * 后端会抛 400 并把原因写在 message 里，
 * 调用方照原样 `message.error(err.message)` 展示即可。
 */
export async function generateDataSourceSchema(
  dsId: string,
): Promise<{ schema_version?: number; stats?: Record<string, unknown>; warnings?: string[] }> {
  return getData(request.post(`/knowledge-base/data-sources/${dsId}/generate-schema`));
}

/** 读该数据源的库表结构统计（生成前给用户看一眼有多少表）。 */
export async function readDataSourceCatalog(dsId: string): Promise<{
  engine: string;
  database: string;
  stats: { tables: number; columns: number; columns_with_comment: number };
}> {
  return getData(request.get(`/knowledge-base/data-sources/${dsId}/catalog`));
}

// ── 数据源同步/接收文档（后端 /documents 暂不按 ds 过滤，前端按 metadata 过滤）──
// 返回与手动上传页一致的 ManualDocItem，便于复用状态链渲染 / 查看 / 删除。
export async function listDataSourceDocuments(
  kbId: string,
  dsId: string,
  t?: (key: string) => string,
): Promise<ManualDocItem[]> {
  const res = await getData<{ items: any[] }>(
    request.get('/knowledge-base/documents', { params: { knowledge_base_id: kbId, page: 1, page_size: 100 } }),
  );
  const _t = t || ((key: string) => key);
  const fmtSize = (b?: number) =>
    typeof b === 'number' ? (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024)} KB`) : '-';
  const statusText = (s: string) =>
    s === 'ready'
      ? `${_t('common.statusImported')}·${_t('common.statusParsing')}·${_t('common.statusCompiling')}`
      : s === 'parsing'
        ? `${_t('common.statusImported')}·${_t('common.statusParsing')}`
        : s === 'pending'
          ? _t('common.statusImported')
          : s;
  return (res.items || [])
    .filter((d) => (d.metadata || {}).data_source_id === dsId)
    .map((d) => {
      const meta = d.metadata || {};
      return {
        id: d.id,
        name: d.file_name,
        type: ((d.file_name || '').split('.').pop() || '').toUpperCase(),
        size: fmtSize(d.file_size),
        uploader: (meta.uploader as string) || 'system',
        uploadTime: formatLocalDateTime(d.created_at),
        status: statusText(d.status),
        docStatus: d.status,
        ontologyStatus: d.ontology_status,
        errorMessage: d.error_message,
        ontologyError: d.ontology_error,
        knowledgeBaseId: kbId,
        dataSourceType: d.data_source_type,
        // 数据源同步列表不展示相似提示；后端即便带出也按「无提示」处理。
        similarityHint: null,
      };
    });
}
