import React from 'react';
import { useTranslation } from 'react-i18next';
import { Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { EditOutlined, DeleteOutlined } from '@ant-design/icons';
import type { OntologyConstraint } from '@/types/domainKnowledge';
import { constraintTargetTypeLabelKey } from '@/types/domainKnowledge';

// 首层卡面已由 PageContent 的卡片承担，这里只留分区之间的间距
const sectionStyle: React.CSSProperties = {
  marginBottom: 20,
};

const TARGET_TYPE_TAG: Record<string, string> = {
  entity: 'blue',
  attribute: 'gold',
  relation: 'purple',
};

interface Props {
  data: OntologyConstraint[];
  loading: boolean;
  onEdit: (item: OntologyConstraint) => void;
  onDelete: (item: OntologyConstraint) => void;  canWrite?: boolean;
}

export default function OntologyConstraintSection({ data, loading, canWrite = false, onEdit, onDelete }: Props) {
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
      render: (value: string) => value || <span style={{ color: 'var(--yx-text-muted)' }}>—</span>,
    },
    {
      title: t('compile.constraint.expression'),
      dataIndex: 'expression',
      key: 'expression',
      render: (value: string) => value || <span style={{ color: 'var(--yx-text-muted)' }}>—</span>,
    },
    {
      title: t('compile.constraint.suggestion'),
      dataIndex: 'suggestion',
      key: 'suggestion',
      width: 200,
      render: (value: string) => value || <span style={{ color: 'var(--yx-text-muted)' }}>—</span>,
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
    <div style={sectionStyle}>
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
