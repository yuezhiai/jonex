import React from 'react';
import { useTranslation } from 'react-i18next';
import { Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
} from '@ant-design/icons';
import type { OntologyRelationDef, OntologyRelationType } from '@/types/domainKnowledge';
import { RELATION_TYPE_TAG, RELATION_CARDINALITY_LABEL_KEYS } from './constants';

// 首层卡面已由 PageContent 的卡片承担，这里只留分区之间的间距
const sectionStyle: React.CSSProperties = {
  marginBottom: 20,
};

interface Props {
  data: OntologyRelationDef[];
  loading: boolean;
  onEdit: (r: OntologyRelationDef) => void;
  onDelete: (r: OntologyRelationDef) => void;
  onPrompt: (r: OntologyRelationDef) => void;
  canWrite?: boolean;
}

export default function OntologyRelationSection({
  canWrite = false,
  data,
  loading,
  onEdit,
  onDelete,
  onPrompt,
}: Props) {
  const { t } = useTranslation();

  const relationTypeToCardinality: Record<string, string> = {
    一对一: 'one_to_one',
    一对多: 'one_to_many',
    多对一: 'many_to_one',
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
          style={{ background: 'var(--yx-info-bg)', color: 'var(--yx-accent)', padding: '2px 8px', borderRadius: 4, fontSize: 12 }}
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
          style={{ background: 'var(--yx-success-bg)', color: '#059669', padding: '2px 8px', borderRadius: 4, fontSize: 12 }}
        >
          {v}
        </span>
      ),
    },
    {
      title: t('compile.relation.description'),
      dataIndex: 'description',
      key: 'description',
      render: (v) => <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{v || '—'}</span>,
    },
    {
      title: t('compile.relation.type'),
      dataIndex: 'relationType',
      key: 'relationType',
      width: 90,
      render: (v: OntologyRelationType) => {
        const cardinality = relationTypeToCardinality[v] || 'custom';
        const c = RELATION_TYPE_TAG[cardinality as keyof typeof RELATION_TYPE_TAG] || {
          bg: 'var(--yx-info-bg)',
          color: 'var(--yx-accent)',
        };
        return (
          <span style={{ background: c.bg, color: c.color, padding: '2px 8px', borderRadius: 4, fontSize: 12 }}>
            {t(RELATION_CARDINALITY_LABEL_KEYS[cardinality as keyof typeof RELATION_CARDINALITY_LABEL_KEYS])}
          </span>
        );
      },
    },
    {
      // [jonex] P1：逆关系名（纯声明，不生成反向边）
      title: t('compile.relation.inverseOf'),
      dataIndex: 'inverseOf',
      key: 'inverseOf',
      width: 120,
      render: (v) => <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{v || '—'}</span>,
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
    <div style={sectionStyle}>
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
