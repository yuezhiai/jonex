import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ControlOutlined, PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import type { OntologyConstraint } from '@/types/domainKnowledge';
import { constraintTargetTypeLabelKey } from '@/types/domainKnowledge';

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

const TARGET_TYPE_TAG: Record<string, string> = {
  entity: 'blue',
  attribute: 'gold',
  relation: 'purple',
};

interface Props {
  data: OntologyConstraint[];
  loading: boolean;
  onCreate: () => void;
  onEdit: (item: OntologyConstraint) => void;
  onDelete: (item: OntologyConstraint) => void;  canWrite?: boolean;
}

export default function OntologyConstraintSection({ data, loading, canWrite = false, onCreate, onEdit, onDelete }: Props) {
  const { t } = useTranslation();
  const columns: ColumnsType<OntologyConstraint> = [
    {
      title: t('compile.constraint.name'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (value) => <strong>{value}</strong>,
    },
    {
      title: t('compile.constraint.targetType'),
      dataIndex: 'targetType',
      key: 'targetType',
      width: 100,
      render: (value: string) => (
        <Tag color={TARGET_TYPE_TAG[value] || 'default'}>
          {t(constraintTargetTypeLabelKey[value as keyof typeof constraintTargetTypeLabelKey] || value)}
        </Tag>
      ),
    },
    {
      title: t('compile.constraint.targetObject'),
      key: 'target',
      width: 200,
      render: (_, record) => record.targetLabel || record.targetCode,
    },
    {
      title: t('compile.constraint.constraintType'),
      dataIndex: 'constraintType',
      key: 'constraintType',
      width: 120,
      render: (value: string) => value || <span style={{ color: '#94a3b8' }}>—</span>,
    },
    {
      title: t('compile.constraint.expression'),
      dataIndex: 'expression',
      key: 'expression',
      render: (value: string) => value || <span style={{ color: '#94a3b8' }}>—</span>,
    },
    {
      title: t('compile.constraint.suggestion'),
      dataIndex: 'suggestion',
      key: 'suggestion',
      width: 200,
      render: (value: string) => value || <span style={{ color: '#94a3b8' }}>—</span>,
    },
    {
      title: t('common.operation'),
      key: 'actions',
      width: 160,
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
        </span>
      ),
    },
  ];

  return (
    <div className="config-section" style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={h3Style}>
          <ControlOutlined style={{ color: '#3b82f6' }} /> {t('compile.constraint.sectionTitle')}
        </h3>
        <Button type="primary" icon={<PlusOutlined />} style={{ fontSize: 13 }} onClick={onCreate}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          >
          {t('compile.constraint.createBtn')}
        </Button>
      </div>
      <p style={{ fontSize: 13, color: '#94a3b8', marginBottom: 16 }}>{t('compile.constraint.sectionDesc')}</p>
      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        pagination={false}
        size="middle"
        loading={loading}
        locale={{ emptyText: t('compile.constraint.empty') }}
      />
    </div>
  );
}
