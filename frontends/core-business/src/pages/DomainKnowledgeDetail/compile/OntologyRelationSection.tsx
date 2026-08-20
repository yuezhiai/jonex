import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ShareAltOutlined,
  PlusOutlined,
  ImportOutlined,
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
} from '@ant-design/icons';
import type { OntologyRelationDef, OntologyRelationType } from '@/types/domainKnowledge';
import { RELATION_TYPE_TAG, RELATION_CARDINALITY_LABEL_KEYS } from './constants';

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
  data: OntologyRelationDef[];
  loading: boolean;
  onCreate: () => void;
  onImport: () => void;
  onEdit: (r: OntologyRelationDef) => void;
  onDelete: (r: OntologyRelationDef) => void;
  onPrompt: (r: OntologyRelationDef) => void;
  canWrite?: boolean;
}

export default function OntologyRelationSection({
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

  const relationTypeToCardinality: Record<string, string> = {
    一对一: 'one_to_one',
    一对多: 'one_to_many',
    多对一: 'one_to_many',
    多对多: 'many_to_many',
    自定义: 'custom',
  };

  const columns: ColumnsType<OntologyRelationDef> = [
    {
      title: t('compile.relation.sourceObject'),
      dataIndex: 'sourceObject',
      key: 'sourceObject',
      width: 110,
      render: (v) => (
        <span
          className="tag"
          style={{ background: '#eff6ff', color: '#3b82f6', padding: '2px 8px', borderRadius: 4, fontSize: 12 }}
        >
          {v}
        </span>
      ),
    },
    {
      title: t('compile.relation.name'),
      dataIndex: 'name',
      key: 'name',
      width: 110,
      render: (v) => <strong>{v}</strong>,
    },
    {
      title: t('compile.relation.targetObject'),
      dataIndex: 'targetObject',
      key: 'targetObject',
      width: 110,
      render: (v) => (
        <span
          className="tag"
          style={{ background: '#ecfdf5', color: '#059669', padding: '2px 8px', borderRadius: 4, fontSize: 12 }}
        >
          {v}
        </span>
      ),
    },
    {
      title: t('compile.relation.description'),
      dataIndex: 'description',
      key: 'description',
      render: (v) => <span style={{ fontSize: 12, color: '#64748b' }}>{v || '—'}</span>,
    },
    {
      title: t('compile.relation.type'),
      dataIndex: 'relationType',
      key: 'relationType',
      width: 90,
      render: (v: OntologyRelationType) => {
        const cardinality = relationTypeToCardinality[v] || 'custom';
        const c = RELATION_TYPE_TAG[cardinality as keyof typeof RELATION_TYPE_TAG] || {
          bg: '#eff6ff',
          color: '#3b82f6',
        };
        return (
          <span style={{ background: c.bg, color: c.color, padding: '2px 8px', borderRadius: 4, fontSize: 12 }}>
            {t(RELATION_CARDINALITY_LABEL_KEYS[cardinality as keyof typeof RELATION_CARDINALITY_LABEL_KEYS])}
          </span>
        );
      },
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 80,
      render: (v) => (
        <Tag color={v === 'active' ? 'success' : 'default'}>
          {t(v === 'active' ? 'status.active' : 'status.inactive')}
        </Tag>
      ),
    },
    {
      title: t('common.operation'),
      key: 'actions',
      width: 250,
      render: (_, r) => (
        <span>
          <a className="yx-table-action" style={canWrite ? undefined : { opacity: 0.4, cursor: "not-allowed" }}
            onClick={() => canWrite && onEdit(r)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a
            className="yx-table-action"
            style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', opacity: 0.4, cursor: 'not-allowed' }}
            onClick={() => canWrite && onDelete(r)}
          >
            <DeleteOutlined /> {t('common.delete')}
          </a>
          {/* 提示词功能暂未实现先隐藏 */}
          {/* <a className="yx-table-action" style={{ color: '#8b5cf6' }} onClick={() => onPrompt(r)}><MessageOutlined /> 提示词</a> */}
        </span>
      ),
    },
  ];

  return (
    <div className="config-section" style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={h3Style}>
          <ShareAltOutlined style={{ color: '#8b5cf6' }} /> {t('compile.relation.sectionTitle')}
        </h3>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button type="primary" icon={<PlusOutlined />} style={{ fontSize: 13 }} onClick={onCreate}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          >
            {t('compile.relation.createBtn')}
          </Button>
          <Button icon={<ImportOutlined />} style={{ fontSize: 13 }} onClick={onImport}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          >
            {t('compile.relation.importBtn')}
          </Button>
        </div>
      </div>
      <p style={{ fontSize: 13, color: '#94a3b8', marginBottom: 16 }}>{t('compile.relation.sectionDesc')}</p>
      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        pagination={false}
        size="middle"
        loading={loading}
        locale={{ emptyText: t('compile.relation.empty') }}
      />
    </div>
  );
}
