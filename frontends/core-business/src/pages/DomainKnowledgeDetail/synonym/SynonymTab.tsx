import React, { useState } from 'react';
import { Button, Table, Tag, Space, Popconfirm, message, Empty, Alert } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  PlusOutlined,
  ImportOutlined,
  ExportOutlined,
  SwapOutlined,
  EditOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import type { SynonymGroup, SynonymImportResult } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import { useSynonyms } from '@/hooks/useSynonyms';
import { createSynonym, updateSynonym, deleteSynonym, importSynonyms, exportAllSynonyms } from '@/api/ontologySynonym';
import SynonymFormModal from './SynonymFormModal';
import SynonymImportModal from './SynonymImportModal';

const cardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 14,
  border: '1px solid #eef2f6',
  padding: 24,
  boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
};

const h3Style: React.CSSProperties = {
  margin: 0,
  fontSize: 15,
  fontWeight: 600,
  color: '#0b2b5c',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

/** 生成 CSV 单元格（含逗号/引号/换行时用双引号包裹并转义）。 */
function csvCell(value: string): string {
  if (/[",\n\r，]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

interface Props {
  kbId: string;
  /** KB 写权限（权限矩阵） */
  canWrite?: boolean;
}

export default function SynonymTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const { data, total, page, pageSize, loading, error, refresh, setPage } = useSynonyms(kbId);
  const [formModal, setFormModal] = useState<{ open: boolean; editing: SynonymGroup | null }>({
    open: false,
    editing: null,
  });
  const [importOpen, setImportOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [exporting, setExporting] = useState(false);

  const handleSubmit = async (terms: string[], canonical?: string) => {
    setSubmitting(true);
    try {
      if (formModal.editing) {
        await updateSynonym(formModal.editing.id, terms, canonical);
        message.success(t('common.saveSuccess'));
      } else {
        await createSynonym(kbId, terms, canonical);
        message.success(t('common.saveSuccess'));
      }
      setFormModal({ open: false, editing: null });
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (record: SynonymGroup) => {
    try {
      await deleteSynonym(record.id);
      message.success(t('common.deleteSuccess'));
      refresh();
    } catch (e: any) {
      message.error(e?.message || t('common.deleteFailed'));
    }
  };

  const handleImport = async (groups: string[][]): Promise<SynonymImportResult | null> => {
    setSubmitting(true);
    try {
      const res = await importSynonyms(kbId, groups);
      if (res.created > 0) refresh();
      return res;
    } catch (e: any) {
      message.error(e?.message || t('common.operationFailed'));
      return null;
    } finally {
      setSubmitting(false);
    }
  };

  const handleExport = async () => {
    setExporting(true);
    try {
      const all = await exportAllSynonyms(kbId);
      const lines = all.map((g) => g.terms.map(csvCell).join(','));
      const blob = new Blob([`\ufeff${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `synonyms_${kbId}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      message.error(e?.message || t('common.operationFailed'));
    } finally {
      setExporting(false);
    }
  };

  const columns: ColumnsType<SynonymGroup> = [
    {
      title: t('synonym.columnName'),
      dataIndex: 'terms',
      key: 'terms',
      render: (terms: string[], record) => (
        <Space size={[4, 4]} wrap>
          {(terms || []).map((term) => (
            <Tag key={term} color={record.canonical === term ? 'blue' : 'default'}>
              {term}
              {record.canonical === term ? t('synonym.standard') : ''}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      render: (_, record) => (
        <span>
          <a
            className="yx-table-action"
            style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
            onClick={() => canWrite && setFormModal({ open: true, editing: record })}
          >
            <EditOutlined /> {t('common.edit')}
          </a>
          <Popconfirm
            title={t('synonym.deleteTitle')}
            description={t('synonym.deleteDesc')}
            okText={t('synonym.deleteOkText')}
            cancelText={t('common.cancel')}
            okButtonProps={{ danger: true }}
            disabled={!canWrite}
            onConfirm={() => handleDelete(record)}
          >
            <a
              className="yx-table-action"
              style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', opacity: 0.4, cursor: 'not-allowed' }}
            >
              <DeleteOutlined /> {t('common.delete')}
            </a>
          </Popconfirm>
        </span>
      ),
    },
  ];

  return (
    <div className="config-section" style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={h3Style}>
          <SwapOutlined style={{ color: '#3b82f6' }} /> {t('synonym.title')}
        </h3>
        <Space>
          <Button
            icon={<ImportOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => setImportOpen(true)}
          >
            {t('synonym.import')}
          </Button>
          <Button icon={<ExportOutlined />} loading={exporting} onClick={handleExport}>
            {t('synonym.export')}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => setFormModal({ open: true, editing: null })}
          >
            {t('synonym.create')}
          </Button>
        </Space>
      </div>
      <p style={{ fontSize: 13, color: '#94a3b8', marginBottom: 16 }}>{t('synonym.description')}</p>

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={error}
          action={<a onClick={refresh}>{t('common.retry')}</a>}
        />
      )}

      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        size="middle"
        loading={loading}
        locale={{ emptyText: <Empty description={t('synonym.empty')} /> }}
        pagination={{
          current: page,
          pageSize,
          total,
          showTotal: (totalCount) => t('synonym.totalGroups', { total: totalCount }),
          onChange: setPage,
        }}
      />

      <SynonymFormModal
        open={formModal.open}
        editing={formModal.editing}
        submitting={submitting}
        onCancel={() => setFormModal({ open: false, editing: null })}
        onSubmit={handleSubmit}
      />
      <SynonymImportModal
        open={importOpen}
        submitting={submitting}
        onCancel={() => setImportOpen(false)}
        onImport={handleImport}
      />
    </div>
  );
}
