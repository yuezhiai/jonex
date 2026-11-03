import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Alert, Button, Empty, Modal, Popconfirm, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CloudUploadOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  activateDocumentVersion,
  confirmDocumentVersionUpload,
  discardDocumentVersionUpload,
  getDocumentVersionUrl,
  listDocumentVersions,
  uploadDocumentVersion,
} from '@/api/domainKnowledge';
import type { DocumentVersionItem, VersionSimilarityHint } from '@/api/domainKnowledge';
import { versionSourceLabel } from './versionSourceLabel';

interface Props {
  kbId: string;
  docId: string;
  canWrite: boolean;
  /** 上传成功后通知父级刷新文档详情（文件名/状态会变）。 */
  onVersionApplied: () => void;
}

function formatSize(size: number): string {
  if (!size) return '0 B';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function DocumentVersionsTab({
  kbId,
  docId,
  canWrite,
  onVersionApplied,
}: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<DocumentVersionItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await listDocumentVersions(docId);
      if (my !== seq.current) return;
      setItems(next.items);
    } catch (err: unknown) {
      if (my !== seq.current) return;
      setItems([]);
      setError(
        (err as { message?: string })?.message ||
          t('domainKnowledge.versions.loadFailed'),
      );
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [docId, t]);

  useEffect(() => {
    seq.current += 1;
    setItems([]);
    setError(null);
    void load();
  }, [load]);

  const applyUploaded = async (versionNo: number) => {
    message.success(
      t('domainKnowledge.versions.uploaded', { version: versionNo }),
    );
    await load();
    onVersionApplied();
  };

  /** 询问「是否同一文档的新版本」；取消时删除临时对象。 */
  const askSimilarity = (hint: VersionSimilarityHint) => {
    Modal.confirm({
      title: t('domainKnowledge.versions.similarityConfirmTitle'),
      content: t('domainKnowledge.versions.similarityConfirmBody', {
        name: hint.fileName,
        percent: Math.round(hint.similarity * 100),
      }),
      okText: t('domainKnowledge.versions.similarityConfirmOk'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        setUploading(true);
        try {
          const result = await confirmDocumentVersionUpload(docId, kbId, hint);
          await applyUploaded(result.version.versionNo);
        } catch (err: unknown) {
          message.error(
            (err as { message?: string })?.message ||
              t('domainKnowledge.versions.uploadFailed'),
          );
        } finally {
          setUploading(false);
        }
      },
      onCancel: () => {
        void discardDocumentVersionUpload(docId, hint.storageKey, hint.storageBackend)
          .catch(() => {
            message.warning(t('domainKnowledge.versions.similarityDiscardFailed'));
          });
      },
    });
  };

  const doUpload = async (file: File) => {
    setUploading(true);
    try {
      const result = await uploadDocumentVersion(docId, kbId, file);
      if (result.kind === 'needs_confirmation') {
        askSimilarity(result.hint);
        return;
      }
      await applyUploaded(result.version.versionNo);
    } catch (err: unknown) {
      message.error(
        (err as { message?: string })?.message ||
          t('domainKnowledge.versions.uploadFailed'),
      );
    } finally {
      setUploading(false);
    }
  };

  const handleFileSelected = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    Modal.confirm({
      title: t('domainKnowledge.versions.uploadConfirmTitle'),
      content: t('domainKnowledge.versions.uploadConfirmBody', { name: file.name }),
      okText: t('domainKnowledge.versions.upload'),
      cancelText: t('common.cancel'),
      onOk: () => doUpload(file),
    });
  };

  const handleActivate = async (row: DocumentVersionItem) => {
    setUploading(true);
    try {
      await activateDocumentVersion(docId, row.versionNo);
      message.success(
        t('domainKnowledge.versions.activated', { version: row.versionNo }),
      );
      await load();
      onVersionApplied();
    } catch (err: unknown) {
      message.error(
        (err as { message?: string })?.message ||
          t('domainKnowledge.versions.activateFailed'),
      );
    } finally {
      setUploading(false);
    }
  };

  const handleDownload = async (row: DocumentVersionItem) => {
    try {
      const { url } = await getDocumentVersionUrl(docId, row.versionNo);
      window.open(url, '_blank', 'noopener');
    } catch (err: unknown) {
      message.error(
        (err as { message?: string })?.message ||
          t('domainKnowledge.versions.downloadFailed'),
      );
    }
  };

  const columns: ColumnsType<DocumentVersionItem> = [
    {
      title: t('domainKnowledge.versions.version'),
      dataIndex: 'versionNo',
      width: 90,
      render: (value: number, row) => (
        <Space size={4}>
          <Tag color="blue">v{value}</Tag>
          {row.isCurrent && (
            <Tag color="green">
              {t('domainKnowledge.versions.current')}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('domainKnowledge.versions.fileName'),
      dataIndex: 'fileName',
      ellipsis: true,
    },
    {
      title: t('domainKnowledge.versions.size'),
      dataIndex: 'fileSize',
      width: 100,
      render: (value: number) => formatSize(value),
    },
    {
      title: t('domainKnowledge.versions.source'),
      dataIndex: 'source',
      width: 110,
      render: (value: string) => versionSourceLabel(value, t),
    },
    {
      title: t('domainKnowledge.versions.createdBy'),
      dataIndex: 'createdBy',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('domainKnowledge.versions.createdAt'),
      dataIndex: 'createdAt',
      width: 180,
      render: formatTime,
    },
    {
      title: '',
      key: 'actions',
      width: 240,
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => void handleDownload(row)}
          >
            {t('domainKnowledge.versions.download')}
          </Button>
          {canWrite && !row.isCurrent && (
            <Popconfirm
              title={t('domainKnowledge.versions.activateConfirm', {
                version: row.versionNo,
              })}
              okText={t('domainKnowledge.versions.activate')}
              onConfirm={() => void handleActivate(row)}
            >
              <Button size="small" type="link">
                {t('domainKnowledge.versions.activate')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        {canWrite && (
          <>
            <Button
              type="primary"
              icon={<CloudUploadOutlined />}
              loading={uploading}
              onClick={() => fileInputRef.current?.click()}
            >
              {t('domainKnowledge.versions.upload')}
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              style={{ display: 'none' }}
              onChange={handleFileSelected}
            />
          </>
        )}
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
      </Space>
      <Alert type="info" showIcon title={t('domainKnowledge.versions.note')} />
      {error ? (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            <Button size="small" onClick={() => void load()}>
              {t('common.refresh')}
            </Button>
          }
        />
      ) : (
        <Table<DocumentVersionItem>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={false}
          scroll={{ x: 900 }}
          locale={{
            emptyText: <Empty description={t('domainKnowledge.versions.empty')} />,
          }}
        />
      )}
    </Space>
  );
}
