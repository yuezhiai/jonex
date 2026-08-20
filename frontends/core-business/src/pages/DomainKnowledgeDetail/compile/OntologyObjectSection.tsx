import React from 'react';
import './index.scss';
import { useTranslation } from 'react-i18next';
import { Button, Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  AppstoreOutlined,
  PlusOutlined,
  ImportOutlined,
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
} from '@ant-design/icons';
import type { OntologyAttribute, OntologyObjectDef } from '@/types/domainKnowledge';

const cardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 14,
  border: '1px solid #eef2f6',
  padding: 24,
  marginBottom: 20,
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

interface Props {
  data: OntologyObjectDef[];
  loading: boolean;
  onCreate: () => void;
  onImport: () => void;
  onEdit: (item: OntologyObjectDef) => void;
  onDelete: (item: OntologyObjectDef) => void;
  onPrompt: (item: OntologyObjectDef) => void;  canWrite?: boolean;
}

function AttrInlineTable({ attrs, t }: { attrs: OntologyAttribute[]; t: (key: string, opts?: any) => string }) {
  if (!attrs.length) return <span style={{ fontSize: 12, color: '#94a3b8' }}>{t('compile.object.noAttributes')}</span>;
  return (
    <div style={{ margin: 0, padding: 0 }}>
      <Table<OntologyAttribute>
        className="attr-inline-table"
        columns={[
          {
            title: t('compile.object.inlineName'),
            dataIndex: 'name',
            key: 'name',
            width: 120,
            render: (val: string) => <span style={{ fontSize: 12 }}>{val}</span>,
          },
          {
            title: t('compile.object.inlineDescription'),
            dataIndex: 'description',
            key: 'description',
            render: (val: string | null) => <span style={{ fontSize: 12 }}>{val || '—'}</span>,
          },
          {
            title: t('compile.object.inlineType'),
            dataIndex: 'type',
            key: 'type',
            width: 80,
            render: (val: string) => <span style={{ fontSize: 12 }}>{val}</span>,
          },
          {
            title: t('compile.object.inlinePrimaryKey'),
            dataIndex: 'isPrimaryKey',
            key: 'isPrimaryKey',
            width: 70,
            render: (val: boolean) => <span style={{ fontSize: 12 }}>{val ? t('common.yes') : t('common.no')}</span>,
          },
        ]}
        dataSource={attrs}
        rowKey={(r) => r.id || r.name}
        pagination={false}
        size="small"
        showHeader={false}
        style={{ marginInline: 0 }}
      />
    </div>
  );
}

export default function OntologyObjectSection({
  canWrite = false,
  data,
  loading,
  onCreate,
  onImport,
  onEdit,
  onDelete,
  onPrompt,
}: Props) {
  const { t } = useTranslation();
  const columns: ColumnsType<OntologyObjectDef> = [
    {
      title: t('compile.object.name'),
      dataIndex: 'name',
      key: 'name',
      width: 180,
      render: (value) => <strong>{value}</strong>,
    },
    {
      title: t('compile.object.description'),
      dataIndex: 'description',
      key: 'description',
      width: 220,
      render: (value: string) => value || <span style={{ color: '#94a3b8' }}>{t('compile.object.noneFallback')}</span>,
    },
    {
      title: t('compile.object.requirement'),
      dataIndex: 'requirement',
      key: 'requirement',
      width: 220,
      render: (value: string) => value || <span style={{ color: '#94a3b8' }}>{t('compile.object.noneFallback')}</span>,
    },
    {
      title: t('compile.object.attributes'),
      dataIndex: 'attributes',
      key: 'attributes',
      align: 'left',
      width: 360,
      render: (attrs: OntologyAttribute[]) => <AttrInlineTable attrs={attrs || []} t={t} />,
    },
    {
      title: t('common.operation'),
      key: 'actions',
      width: 250,
      render: (_, record) => (
        <span>
          <a className="yx-table-action" style={canWrite ? undefined : { opacity: 0.4, cursor: "not-allowed" }}
            onClick={() => canWrite && onEdit(record)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a
            className="yx-table-action"
            style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', opacity: 0.4, cursor: 'not-allowed' }}
            onClick={() => canWrite && onDelete(record)}
          >
            <DeleteOutlined /> {t('common.delete')}
          </a>
          {/* 提示词功能暂未实现先隐藏 */}
          {/* <a className="yx-table-action" style={{ color: '#8b5cf6' }} onClick={() => onPrompt(record)}><MessageOutlined /> 提示词</a> */}
        </span>
      ),
    },
  ];

  return (
    <div className="config-section" style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={h3Style}>
          <AppstoreOutlined style={{ color: '#3b82f6' }} /> {t('compile.tabObjectDef')}
        </h3>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button type="primary" icon={<PlusOutlined />} style={{ fontSize: 13 }} onClick={onCreate}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          >
            {t('compile.object.createBtn')}
          </Button>
          <Button icon={<ImportOutlined />} style={{ fontSize: 13 }} onClick={onImport}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          >
            {t('compile.object.importBtn')}
          </Button>
        </div>
      </div>
      <p style={{ fontSize: 13, color: '#94a3b8', marginBottom: 16 }}>{t('compile.object.sectionDesc')}</p>
      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        pagination={false}
        size="middle"
        loading={loading}
        locale={{ emptyText: t('compile.object.empty') }}
      />
    </div>
  );
}
