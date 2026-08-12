import React from 'react';
import { Table } from 'antd';
import type { ManualDocItem } from '@/types/domainKnowledge';
import { useDocumentViewer } from '@/components/DocumentViewer';
import DocumentStatusBadge from '@/components/DocumentStatusBadge';
import { useTranslation } from 'react-i18next';
import Space from 'antd/es/space';
import Tag from 'antd/es/tag';

type StepState = 'done' | 'active' | 'pending';
const stepColors: Record<StepState, { bg: string; text: string }> = {
  done: { bg: '#ecfdf5', text: '#10b981' },
  active: { bg: '#eff6ff', text: '#3b82f6' },
  pending: { bg: '#f1f5f9', text: '#cbd5e1' },
};

function getStepStates(status: string): StepState[] {
  const steps = status.split('·').map((s) => s.trim());
  const result: StepState[] = ['pending', 'pending', 'pending'];
  for (let i = 0; i < Math.min(steps.length, 3); i++) {
    result[i] = steps[i].includes('中') ? 'active' : 'done';
  }
  return result;
}

function getStatusSteps(t: (key: string) => string) {
  return [t('status.imported'), t('status.parsing'), t('status.compiling')];
}

function renderStatus(v: string, t: (key: string) => string) {
  // 非链式状态（failed 等）
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
}

interface Props {
  kbId: string;
  docs: ManualDocItem[];
  loading?: boolean;
  /** 数据变更后回调（删除成功等），供页面刷新列表 */
  onChanged?: () => void;
  /** 空列表文案 */
  emptyText?: string;
  /** 分页配置，默认每页 10 条 */
  pageSize?: number;
  /** 是否显示删除按钮 */
  showDelete?: boolean;
  /** 删除回调（确认弹窗由父组件处理） */
  onDelete?: (doc: ManualDocItem) => void;
  /** [jonex] 知识库类型：openkb 时文档状态列按 llm_wiki_compile_status 显示。 */
  kbType?: string;
}

/**
 * 数据源文档列表表格（4 种数据源统一复用）。
 * 列：文档名称 / 类型 / 大小 / 上传人 / 上传时间 / 状态 / 操作（查看 + 可选删除）。
 */
export default function DataSourceDocTable({
  kbId,
  docs,
  loading,
  onChanged,
  emptyText,
  pageSize = 10,
  showDelete = false,
  onDelete,
  kbType,
}: Props) {
  const { t } = useTranslation();
  const resolvedEmptyText = emptyText ?? t('common.noData');
  const { openDocument, viewer } = useDocumentViewer();

  // 查看：全部走统一弹层（音视频/图片/PDF/文本/其他）
  const handleView = (doc: ManualDocItem) => {
    openDocument({ docId: doc.id, fileName: doc.name });
  };

  const columns = [
    {
      title: t('common.fileName'),
      dataIndex: 'name',
      key: 'name',
      width: 240,
      render: (v: string) => <a className="yx-table-action">{v}</a>,
    },
    {
      title: t('common.fileType'),
      dataIndex: 'type',
      key: 'type',
      width: 80,
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
      key: 'actions',
      width: showDelete ? 130 : 80,
      render: (_: unknown, record: ManualDocItem) => (
        <Space size={8}>
          <a className="yx-table-action" onClick={() => handleView(record)}>
            {t('common.preview')}
          </a>
          {showDelete && onDelete && (
            <a className="yx-table-action" style={{ color: '#ef4444' }} onClick={() => onDelete(record)}>
              {t('common.delete')}
            </a>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <Table
        columns={columns}
        dataSource={docs}
        rowKey="id"
        size="middle"
        loading={loading}
        pagination={{ pageSize }}
        locale={{ emptyText: resolvedEmptyText }}
      />
      {viewer}
    </>
  );
}
