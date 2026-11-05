import React from 'react';
import './index.scss';
import { useTranslation } from 'react-i18next';
import { Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  EditOutlined,
  DeleteOutlined,
  MessageOutlined,
} from '@ant-design/icons';
import type { OntologyAttribute, OntologyObjectDef } from '@/types/domainKnowledge';

// 首层卡面已由 PageContent 的卡片承担，这里只留分区之间的间距
const sectionStyle: React.CSSProperties = {
  marginBottom: 20,
};

interface Props {
  data: OntologyObjectDef[];
  loading: boolean;
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
      render: (value, row) => {
        // 有抽取期工具声明（catalog:）的对象标一下，便于找到在哪配置的
        const n = [...(row.extractionSkills?.refine || []), ...(row.extractionSkills?.constrain || [])].filter(
          (s) => typeof s?.skill === 'string' && s.skill.startsWith('catalog:'),
        ).length;
        return (
          <div>
            <strong>{value}</strong>
            {n > 0 && (
              <div>
                <Tag color="purple" style={{ marginTop: 4, fontSize: 11 }}>
                  {t('compile.extractTool.decl.listTag', { count: n })}
                </Tag>
              </div>
            )}
          </div>
        );
      },
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
    <div style={sectionStyle}>
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
