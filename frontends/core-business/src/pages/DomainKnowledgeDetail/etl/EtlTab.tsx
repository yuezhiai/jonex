/**
 * 知识库设置 →「ETL 集成」页签。
 *
 * 这里只做**来源登记与运维**，不碰血缘边本身：
 * - 列表：本 KB 下 `access_type=etl` 的数据源（file / openlineage 两档）；
 * - 新建：登记一个 ETL 来源 + 目标 database 数据源（`EtlSourceModal`）；
 * - 状态：`lastSyncAt / lastSyncStatus / lastSyncMessage` 原样展示 ——
 *   同步失败的原因（后端写的 imported/skipped/errors 或异常 message）是用户
 *   唯一能看懂「为什么没进来」的地方，不许吞成通用文案；
 * - file 类型：上传 JSON/CSV 血缘文件（`importLineageFile`）；
 * - 全部：重置 ingest key（明文只回一次，用 Alert 留住）；
 * - 删除：Popconfirm 二次确认（只删来源行，已导入的血缘边保留）。
 *
 * 四态齐：loading / error（可重试）/ empty / success，另有刷新。
 * 写入面仅在 `canWrite` 时出现（新增 / 上传 / 重置 / 删除），同 `LineageTab`。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  message,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  BranchesOutlined,
  DeleteOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  deleteDataSource,
  importLineageFile,
  listDataSources,
  resetIngestKey,
  type LineageImportResult,
} from '@/api/dataSource';
import type { DataSourceInstance } from '@/types/dataSource';
import { formatLocalDateTime } from '@/utils/dateTime';
import { canImportFile, etlKindLabelKey, isEtlSource } from './etlModel';
import EtlSourceModal from './EtlSourceModal';

const { Text } = Typography;

interface Props {
  kbId: string;
  /** 固定目标数据源（从数据源卡片进入时用）：只列绑定到该源的 ETL 来源。 */
  targetDataSourceId?: string;
  /** KB 写权限：false 时一个写入按钮都不渲染（不是靠后端拦）。 */
  canWrite?: boolean;
}

/** 最近一次导入的回执（含错误明细，用于在页面上如实展示而不是只弹个 toast）。 */
interface ImportFeedback {
  sourceName: string;
  result: LineageImportResult;
}

/** 最近一次重置出来的明文 key（只回这一次，留到用户关掉 Alert 为止）。 */
interface NewKeyFeedback {
  sourceName: string;
  key: string;
  url?: string;
}

export default function EtlTab({ kbId, canWrite = false,
                                 targetDataSourceId }: Props) {
  const { t } = useTranslation();

  const [allSources, setAllSources] = useState<DataSourceInstance[]>([]);
  const [sources, setSources] = useState<DataSourceInstance[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [modalOpen, setModalOpen] = useState(false);
  const [uploadingId, setUploadingId] = useState<string | null>(null);
  const [resettingId, setResettingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [lastImport, setLastImport] = useState<ImportFeedback | null>(null);
  const [newKey, setNewKey] = useState<NewKeyFeedback | null>(null);

  /** 每行一个隐藏的 file input；上传按钮只负责触发它。 */
  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  /** 请求序号：快速切换 KB / 连续刷新时，丢掉上一轮的迟到响应，避免旧列表覆盖新列表。 */
  const sourcesReqRef = useRef(0);

  const load = useCallback(
    async (silent = false) => {
      const req = ++sourcesReqRef.current;
      if (!silent) setLoading(true);
      setError('');
      try {
        const list = await listDataSources(kbId);
        if (sourcesReqRef.current !== req) return;
        setAllSources(list);
        setSources(list.filter(isEtlSource).filter((ds) =>
          !targetDataSourceId
          || String((ds.configJson || {}).data_source_id || '') === targetDataSourceId));
      } catch (e: any) {
        if (sourcesReqRef.current !== req) return;
        setError(e?.message || t('domainKnowledge.etl.loadFailed'));
      } finally {
        // 只让「最新那一发」收 loading：迟到的旧请求不能把新一轮的转圈关掉。
        if (sourcesReqRef.current === req) setLoading(false);
      }
    },
    [kbId, t, targetDataSourceId],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // 目标数据源名称：ETL 行只存 id，展示要回查（全量列表已拉，无需二次请求）。
  const sourceById = useMemo(
    () => new Map(allSources.map((ds) => [ds.id, ds])),
    [allSources],
  );
  const databaseSources = useMemo(
    () => allSources.filter((ds) => ds.accessType === 'database'
      && (!targetDataSourceId || ds.id === targetDataSourceId)),
    [allSources, targetDataSourceId],
  );

  const handleUpload = async (ds: DataSourceInstance, file: File) => {
    setUploadingId(ds.id);
    try {
      const result = await importLineageFile(ds.id, file);
      setLastImport({ sourceName: ds.name, result });
      if (result.error_count > 0) {
        message.warning(t('domainKnowledge.etl.importPartial'));
      } else {
        message.success(t('domainKnowledge.etl.importSuccess'));
      }
      // 导入会推进 last_sync_*（后端 `import_lineage_file` 收尾写入），静默刷新列表。
      await load(true);
    } catch (err: any) {
      // 后端 message 原样透出（UTF-8 / 行数上限 / 不是 file 类型 / 列不存在…）
      message.error(err?.message || t('domainKnowledge.etl.importFailed'));
    } finally {
      setUploadingId(null);
    }
  };

  const handleReset = async (ds: DataSourceInstance) => {
    setResettingId(ds.id);
    try {
      const updated = await resetIngestKey(ds.id);
      if (updated.ingestKey) {
        setNewKey({ sourceName: ds.name, key: updated.ingestKey, url: updated.ingestUrl });
      } else {
        message.error(t('domainKnowledge.etl.resetFailed'));
      }
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.etl.resetFailed'));
    } finally {
      setResettingId(null);
    }
  };

  const handleDelete = async (ds: DataSourceInstance) => {
    setDeletingId(ds.id);
    try {
      await deleteDataSource(ds.id);
      await load(true);
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.etl.deleteFailed'));
    } finally {
      setDeletingId(null);
    }
  };

  const renderLastSync = (ds: DataSourceInstance) => {
    if (!ds.lastSyncAt) {
      return <Text type="secondary">{t('domainKnowledge.etl.neverSynced')}</Text>;
    }
    const status = ds.lastSyncStatus || '';
    // 只对已知两档上色/翻译；未知状态原样显示，不猜也不吞（后端加档位时要能看见）。
    const statusColor = status === 'success' ? 'green' : status === 'failed' ? 'red' : 'default';
    const statusLabel =
      status === 'success'
        ? t('domainKnowledge.etl.syncSuccess')
        : status === 'failed'
          ? t('domainKnowledge.etl.syncFailed')
          : status;
    return (
      <Space direction="vertical" size={0} style={{ maxWidth: 280 }}>
        <span>
          <span style={{ marginInlineEnd: 8 }}>{formatLocalDateTime(ds.lastSyncAt)}</span>
          {status && <Tag color={statusColor}>{statusLabel}</Tag>}
        </span>
        {ds.lastSyncMessage && (
          <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: ds.lastSyncMessage }}>
            {ds.lastSyncMessage}
          </Text>
        )}
      </Space>
    );
  };

  const columns: ColumnsType<DataSourceInstance> = [
    {
      title: t('domainKnowledge.etl.name'),
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: t('domainKnowledge.etl.kind'),
      key: 'kind',
      width: 150,
      render: (_: unknown, ds) => {
        const kind = ds.configJson?.etl_kind as string | undefined;
        // 未知 kind 不上「文件导入」的绿色（会误导），用默认色 + unknown 文案。
        const color = kind === 'openlineage' ? 'geekblue' : kind === 'file' ? 'green' : 'default';
        return <Tag color={color}>{t(etlKindLabelKey(kind))}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.etl.targetSource'),
      key: 'target',
      render: (_: unknown, ds) => {
        const id = ds.configJson?.data_source_id as string | undefined;
        const target = id ? sourceById.get(id) : undefined;
        if (target) return <span>{target.name}</span>;
        // 目标被删 / 配置缺 id：如实显示，不渲染成空白让人以为「没有目标」
        return <Tag color="red">{t('domainKnowledge.etl.targetMissing')}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.etl.lastSync'),
      key: 'lastSync',
      render: (_: unknown, ds) => renderLastSync(ds),
    },
  ];

  if (canWrite) {
    columns.push({
      title: t('common.actions'),
      key: 'actions',
      width: 330,
      render: (_: unknown, ds) => (
        <Space size={0}>
          {canImportFile(ds) && (
            <>
              <Button
                type="text"
                size="small"
                icon={<UploadOutlined />}
                loading={uploadingId === ds.id}
                onClick={() => fileInputRefs.current[ds.id]?.click()}
              >
                {t('domainKnowledge.etl.uploadFile')}
              </Button>
              <input
                ref={(el) => {
                  fileInputRefs.current[ds.id] = el;
                }}
                type="file"
                accept=".json,.csv"
                style={{ display: 'none' }}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  // 清空 value：同一个文件连传两次也要能再次触发 onChange。
                  e.target.value = '';
                  if (file) void handleUpload(ds, file);
                }}
              />
            </>
          )}
          <Button
            type="text"
            size="small"
            icon={<KeyOutlined />}
            loading={resettingId === ds.id}
            onClick={() => void handleReset(ds)}
          >
            {t('domainKnowledge.etl.resetKey')}
          </Button>
          {/* 删除二次确认：误删 ETL 来源会让推送端 key 直接失效（边保留，来源没了） */}
          <Popconfirm
            title={t('domainKnowledge.etl.deleteConfirm')}
            onConfirm={() => void handleDelete(ds)}
            okText={t('domainKnowledge.etl.delete')}
            cancelText={t('domainKnowledge.etl.cancel')}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              loading={deletingId === ds.id}
            >
              {t('domainKnowledge.etl.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    });
  }

  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        title={t('domainKnowledge.etl.loadFailed')}
        description={error}
        action={
          <Button size="small" onClick={() => void load()}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <BranchesOutlined /> {t('domainKnowledge.etl.title')}
          <span className="yx-kb-chip-blue">
            {t('domainKnowledge.etl.count', { total: sources.length })}
          </span>
        </h3>
        <Space>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            {t('common.refresh')}
          </Button>
          {canWrite && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
              {t('domainKnowledge.etl.addSource')}
            </Button>
          )}
        </Space>
      </div>

      <p className="yx-kb-graph-desc">
        {t(canWrite ? 'domainKnowledge.etl.writableHint' : 'domainKnowledge.etl.readonlyHint')}
      </p>

      {lastImport && (
        <Alert
          type={lastImport.result.error_count > 0 ? 'warning' : 'success'}
          showIcon
          closable
          style={{ marginBottom: 12 }}
          title={t('domainKnowledge.etl.importResultTitle', { name: lastImport.sourceName })}
          onClose={() => setLastImport(null)}
          description={
            <div>
              <div>
                {t('domainKnowledge.etl.importSummary', {
                  imported: lastImport.result.imported,
                  skipped: lastImport.result.skipped,
                  errors: lastImport.result.error_count,
                  total: lastImport.result.total,
                })}
              </div>
              {lastImport.result.errors.length > 0 && (
                <ul style={{ margin: '8px 0 0', paddingInlineStart: 20 }}>
                  {lastImport.result.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              )}
              {lastImport.result.error_count > lastImport.result.errors.length && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t('domainKnowledge.etl.importErrorsTruncated', {
                    shown: lastImport.result.errors.length,
                    total: lastImport.result.error_count,
                  })}
                </Text>
              )}
            </div>
          }
        />
      )}

      {newKey && (
        <Alert
          type="success"
          showIcon
          closable
          style={{ marginBottom: 12 }}
          title={t('domainKnowledge.etl.newKeyTitle', { name: newKey.sourceName })}
          onClose={() => setNewKey(null)}
          description={
            <div>
              <p style={{ margin: '0 0 8px' }}>{t('domainKnowledge.etl.keyOnce')}</p>
              {newKey.url && (
                <div style={{ marginBottom: 8 }}>
                  {t('domainKnowledge.etl.ingestUrlLabel')}
                  <Text copyable={{ tooltips: [t('domainKnowledge.etl.copy'), t('domainKnowledge.etl.copied')] }}>
                    {newKey.url}
                  </Text>
                </div>
              )}
              <div>
                {t('domainKnowledge.etl.ingestKeyLabel')}
                <Text
                  copyable={{ text: newKey.key, tooltips: [t('domainKnowledge.etl.copy'), t('domainKnowledge.etl.copied')] }}
                  code
                >
                  {newKey.key}
                </Text>
              </div>
            </div>
          }
        />
      )}

      {sources.length === 0 ? (
        <Empty description={t('domainKnowledge.etl.empty')} />
      ) : (
        <Table<DataSourceInstance>
          rowKey="id"
          columns={columns}
          dataSource={sources}
          pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
          size="middle"
        />
      )}

      {canWrite && (
        <EtlSourceModal
          open={modalOpen}
          kbId={kbId}
          databaseSources={databaseSources}
          fixedDataSourceId={targetDataSourceId}
          onClose={() => setModalOpen(false)}
          onCreated={() => void load(true)}
        />
      )}
    </div>
  );
}
