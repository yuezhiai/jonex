import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Alert, Button, Empty, Modal, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CloudUploadOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  getDocumentVersionUrl,
  listDocumentVersions,
  uploadDocumentVersion,
} from '@/api/domainKnowledge';
import type { DocumentVersionItem } from '@/api/domainKnowledge';

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

  const doUpload = async (file: File) => {
    setUploading(true);
    try {
      const result = await uploadDocumentVersion(docId, kbId, file);
      message.success(
        t('domainKnowledge.versions.uploaded', { version: result.version.versionNo }),
      );
      await load();
      onVersionApplied();
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
      render: (value: number) => <Tag color="blue">v{value}</Tag>,
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
      render: (value: string) =>
        value === 'backfill'
          ? t('domainKnowledge.versions.sourceBackfill')
          : t('domainKnowledge.versions.sourceUpload'),
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
      width: 120,
      render: (_: unknown, row) => (
        <Button
          size="small"
          icon={<DownloadOutlined />}
          onClick={() => void handleDownload(row)}
        >
          {t('domainKnowledge.versions.download')}
        </Button>
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
