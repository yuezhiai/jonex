import React from 'react';
import { Tag, Space, Button, Dropdown } from 'antd';
import type { MenuProps } from 'antd';
import {
  FileOutlined,
  MoreOutlined,
  EyeOutlined,
  TagOutlined,
  ReloadOutlined,
  BuildOutlined,
  DeleteOutlined,
  FolderOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import type { ColumnsType } from 'antd/es/table';
import type { ManualDocItem } from '@/types/domainKnowledge';
import type { AccessMethodItem } from '@/types/dataSource';
import DocumentStatusBadge from '@/components/DocumentStatusBadge';
import { accessMethodDisplayName } from '@/utils/dataSourceDisplay';

type StepState = 'done' | 'active' | 'pending';
const stepColors: Record<StepState, { bg: string; text: string }> = {
  done: { bg: '#ecfdf5', text: '#10b981' },
  active: { bg: '#eff6ff', text: '#3b82f6' },
  pending: { bg: '#f1f5f9', text: '#cbd5e1' },
};

const getStepStates = (status: string): StepState[] => {
  const steps = status.split('·').map((s) => s.trim());
  const result: StepState[] = ['pending', 'pending', 'pending'];
  for (let i = 0; i < Math.min(steps.length, 3); i++) {
    result[i] = steps[i].includes('中') ? 'active' : 'done';
  }
  return result;
};

function getStatusSteps(t: TFunction) {
  return [t('status.imported'), t('status.parsing'), t('status.compiling')];
}

export const renderStatus = (v: string, t: TFunction) => {
  if (!v.includes('·')) {
    const isError = v === 'failed';
    return (
      <Tag
        style={{
          fontSize: 11,
          padding: '2px 8px',
          border: 'none',
          fontWeight: 500,
          background: isError ? '#fef2f2' : '#f1f5f9',
          color: isError ? '#ef4444' : '#64748b',
        }}
      >
        {isError ? t('status.failed') : v}
      </Tag>
    );
  }
  const states = getStepStates(v);
  const STATUS_STEPS = getStatusSteps(t);
  return (
    <Space size={4}>
      {STATUS_STEPS.map((label, i) => {
        const s = states[i];
        return (
          <React.Fragment key={i}>
            <span
              style={{
                display: 'inline-block',
                fontSize: 11,
                padding: '2px 8px',
                border: 'none',
                fontWeight: 500,
                background: stepColors[s].bg,
                color: stepColors[s].text,
                borderRadius: 4,
                lineHeight: '18px',
              }}
            >
              {label}
            </span>
            {i < STATUS_STEPS.length - 1 && <span style={{ color: '#d1d5db', fontSize: 10 }}>·</span>}
          </React.Fragment>
        );
      })}
    </Space>
  );
};

export const typeOptions = (t: TFunction) => [
  { value: 'all', label: t('common.typeAll') },
  { value: 'pdf', label: 'PDF' },
  { value: 'docx', label: 'DOCX' },
  { value: 'xlsx', label: 'XLSX' },
  { value: 'pptx', label: 'PPTX' },
  { value: 'video', label: t('common.video') },
  { value: 'audio', label: t('common.audio') },
];

export const statusOptions = (t: TFunction) => [
  { value: 'all', label: t('common.statusAll') },
  {
    value: 'compiled',
    label: t('common.statusCompiled'),
  },
  { value: 'parsing', label: t('status.compiling') },
  {
    value: 'pending',
    label: t('common.statusPending'),
  },
];

const actionItems = (record: ManualDocItem, handlers: ActionHandlers, t: TFunction, canWrite: boolean): MenuProps['items'] => [
  {
    key: 'view',
    icon: <EyeOutlined />,
    label: t('common.view'),
    onClick: () => handlers.onView(record),
  },
  {
    key: 'tag',
    icon: <TagOutlined />,
    label: t('common.tag'),
    disabled: !canWrite,
    onClick: () => canWrite && handlers.onTag(record),
  },
  {
    key: 'reparse',
    icon: <ReloadOutlined />,
    label: t('common.reparse'),
    disabled: !canWrite,
    onClick: () => canWrite && handlers.onReparse(record),
  },
  {
    key: 'recompile',
    icon: <BuildOutlined />,
    label: t('common.recompile'),
    disabled: !canWrite,
    onClick: () => canWrite && handlers.onRecompile(record),
  },
  {
    key: 'move',
    icon: <FolderOutlined />,
    label: t('common.move'),
    onClick: () => handlers.onMove(record),
  },
  {
    key: 'delete',
    icon: <DeleteOutlined style={{ color: '#ef4444' }} />,
    label: t('common.delete'),
    style: { color: '#ef4444' },
    disabled: !canWrite,
    onClick: () => canWrite && handlers.onDelete(record),
  },
];

interface ActionHandlers {
  onView: (record: ManualDocItem) => void;
  onTag: (record: ManualDocItem) => void;
  onViewResult: (record: ManualDocItem) => void;
  onReparse: (record: ManualDocItem) => void;
  onRecompile: (record: ManualDocItem) => void;
  onMove: (record: ManualDocItem) => void;
  onDelete: (record: ManualDocItem) => void;
}

export const createColumns = (
  t: TFunction,
  handlers: ActionHandlers,
  accessMethods?: AccessMethodItem[],
  kbType?: string,
  canWrite = false,
): ColumnsType<ManualDocItem> => {
  // 构建 accessType → name 映射
  const typeNameMap = new Map<string, string>(
    (accessMethods || []).map((m) => [m.accessType, accessMethodDisplayName(m, t)]),
  );

  return [
    {
      title: t('common.fileName'),
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      width: 200,
      render: (name: string, record: ManualDocItem) => (
        <Space>
          <FileOutlined style={{ color: '#3b82f6' }} />
          <span
            style={{
              color: '#3b82f6',
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
            onClick={(e) => {
              e.stopPropagation();
              handlers.onViewResult(record);
            }}
          >
            {name}
          </span>
        </Space>
      ),
    },
    {
      title: t('common.fileType'),
      dataIndex: 'type',
      key: 'type',
      width: 80,
    },
    {
      title: t('common.dataSource'),
      dataIndex: 'dataSourceType',
      key: 'dataSourceType',
      width: 100,
      render: (v: string) => {
        if (!v) return <span style={{ color: '#94a3b8' }}>—</span>;
        const displayName = typeNameMap.get(v);
        return (
          <Tag
            style={{
              fontSize: 11,
              padding: '2px 8px',
              border: 'none',
              background: '#f0f9ff',
              color: '#0284c7',
              fontWeight: 500,
            }}
          >
            {displayName || v}
          </Tag>
        );
      },
    },
    {
      title: t('common.fileSize'),
      dataIndex: 'size',
      key: 'size',
      width: 90,
    },
    {
      title: t('common.uploader'),
      dataIndex: 'uploader',
      key: 'uploader',
      width: 90,
    },
    {
      title: t('common.uploadTime'),
      dataIndex: 'uploadTime',
      key: 'uploadTime',
      width: 160,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 200,
      render: (_: unknown, record: ManualDocItem) => (
        <DocumentStatusBadge
          docStatus={record.docStatus}
          ontologyStatus={record.ontologyStatus}
          kbType={kbType}
          llmWikiCompileStatus={record.llmWikiCompileStatus}
          llmWikiCompileError={record.llmWikiCompileError}
          errorMessage={record.errorMessage}
          ontologyError={record.ontologyError}
        />
      ),
    },
    {
      title: t('common.actions'),
      key: 'action',
      width: 80,
      align: 'center',
      render: (_: unknown, record: ManualDocItem) => (
        <Dropdown menu={{ items: actionItems(record, handlers, t, canWrite) }} trigger={['click']} placement="bottomRight">
          <Button type="text" icon={<MoreOutlined />} size="small" />
        </Dropdown>
      ),
    },
  ];
};
