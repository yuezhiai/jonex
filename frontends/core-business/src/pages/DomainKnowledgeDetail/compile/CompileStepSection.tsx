import React from 'react';
import { Button, Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import { OrderedListOutlined, PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import type { CompileStep } from '@/types/domainKnowledge';
import { compileScopeLabelKey, compileTriggerLabelKey } from '@/types/domainKnowledge';

// 首层卡面已由 PageContent 的卡片承担，这里只留分区之间的间距
const sectionStyle: React.CSSProperties = {
  marginBottom: 20,
};
const h3Style: React.CSSProperties = {
  margin: 0,
  fontSize: 15,
  fontWeight: 600,
  color: 'var(--yx-brand-dark)',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

interface Props {
  data: CompileStep[];
  loading: boolean;
  onCreate: () => void;
  onEdit: (s: CompileStep) => void;
  onDelete: (s: CompileStep) => void;
}

export default function CompileStepSection({ data, loading, onCreate, onEdit, onDelete }: Props) {
  const { t } = useTranslation();
  const columns: ColumnsType<CompileStep> = [
    {
      title: t('compile.step.order'),
      dataIndex: 'order',
      key: 'order',
      width: 80,
      align: 'center',
      render: (v) => <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{v}</span>,
    },
    { title: t('compile.step.name'), dataIndex: 'name', key: 'name', width: 110, render: (v) => <strong>{v}</strong> },
    {
      title: t('compile.step.prompt'),
      dataIndex: 'prompt',
      key: 'prompt',
      render: (v) => <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{v}</span>,
    },
    {
      title: t('compile.step.skill'),
      dataIndex: 'skill',
      key: 'skill',
      width: 130,
      render: (v: string) =>
        v ? (
          <span style={{ background: '#f5f3ff', color: '#7c3aed', padding: '2px 8px', borderRadius: 4, fontSize: 12 }}>
            {v}
          </span>
        ) : (
          <span style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>—</span>
        ),
    },
    {
      title: t('compile.step.scope'),
      dataIndex: 'scope',
      key: 'scope',
      width: 100,
      render: (v: CompileStep['scope']) => (
        <span
          style={{
            background: v === 'single' ? 'var(--yx-info-bg)' : 'var(--yx-success-bg)',
            color: v === 'single' ? 'var(--yx-accent)' : '#059669',
            padding: '2px 8px',
            borderRadius: 4,
            fontSize: 12,
          }}
        >
          {t(compileScopeLabelKey[v])}
        </span>
      ),
    },
    {
      title: t('compile.step.trigger'),
      dataIndex: 'trigger',
      key: 'trigger',
      width: 160,
      render: (v: CompileStep['trigger']) => (
        <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{t(compileTriggerLabelKey[v])}</span>
      ),
    },
    {
      title: t('compile.step.template'),
      dataIndex: 'template',
      key: 'template',
      width: 180,
      render: (v) => <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{v}</span>,
    },
    {
      title: t('common.operation'),
      key: 'actions',
      width: 140,
      render: (_, r) => (
        <span>
          <a className="yx-table-action" onClick={() => onEdit(r)}>
            <EditOutlined /> {t('common.edit')}
          </a>
          <a className="yx-table-action" style={{ color: '#ef4444' }} onClick={() => onDelete(r)}>
            <DeleteOutlined /> {t('common.delete')}
          </a>
        </span>
      ),
    },
  ];
  return (
    <div style={sectionStyle}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={h3Style}>
          <OrderedListOutlined style={{ color: '#f97316' }} /> {t('compile.step.settings')}
        </h3>
        <Button type="primary" icon={<PlusOutlined />} style={{ fontSize: 13 }} onClick={onCreate}>
          {t('compile.step.create')}
        </Button>
      </div>
      <p style={{ fontSize: 13, color: 'var(--yx-text-muted)', marginBottom: 12 }}>{t('compile.step.description')}</p>
      <Table
        columns={columns}
        dataSource={data}
        rowKey="id"
        pagination={false}
        size="middle"
        loading={loading}
        locale={{ emptyText: t('compile.step.empty') }}
      />
    </div>
  );
}
