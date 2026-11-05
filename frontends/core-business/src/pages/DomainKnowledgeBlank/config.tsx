import { Tag, Space, Button, Dropdown, Tooltip } from 'antd';
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
  PauseOutlined,
  CaretRightOutlined,
  ExperimentOutlined,
} from '@ant-design/icons';
import type { TFunction } from 'i18next';
import type { ColumnsType } from 'antd/es/table';
import type { ManualDocItem } from '@/types/domainKnowledge';
import type { AccessMethodItem } from '@/types/dataSource';
import DocumentStatusBadge from '@/components/DocumentStatusBadge';
import DocumentQualityBadge from '@/components/DocumentQualityBadge';
import { accessMethodDisplayName } from '@/utils/dataSourceDisplay';

const actionItems = (record: ManualDocItem, handlers: ActionHandlers, t: TFunction, canWrite: boolean, paused?: boolean, onToggleCompilation?: () => void, controlDisabled?: boolean, showModelUpdate = true): MenuProps['items'] => [
  ...(onToggleCompilation ? [{
    key: 'compilation-control',
    icon: paused ? <CaretRightOutlined /> : <PauseOutlined />,
    label: t(`domainKnowledge.wikiCompilation.${paused ? 'resume' : 'pause'}`),
    disabled: !canWrite || controlDisabled,
    onClick: onToggleCompilation,
  }] : []),
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
  ...(showModelUpdate ? [{
    key: 'update-model',
    icon: <ExperimentOutlined />,
    label: t('common.updateModel'),
    disabled: !canWrite,
    onClick: () => canWrite && handlers.onUpdateModel(record),
  }] : []),
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
  onUpdateModel: (record: ManualDocItem) => void;
  onMove: (record: ManualDocItem) => void;
  onDelete: (record: ManualDocItem) => void;
}

export const createColumns = (
  t: TFunction,
  handlers: ActionHandlers,
  accessMethods?: AccessMethodItem[],
  kbType?: string,
  canWrite = false,
  compilationPaused = false,
  onToggleCompilation?: () => void,
  controlDisabled = false,
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
      render: (name: string, record: ManualDocItem) => {
        const hint = record.similarityHint;
        const tip = hint
          ? t('domainKnowledge.similarity.badgeTip', {
              name: hint.targetFileName,
              percent: Math.round(hint.similarity * 100),
            })
          : '';
        return (
          <Space style={{ minWidth: 0 }}>
            <FileOutlined style={{ color: '#3b82f6', flex: 'none' }} />
            <span
              style={{
                color: '#3b82f6',
                cursor: 'pointer',
                textDecoration: 'underline',
                // 长文件名先收缩并省略，给徽标保留位置
                flex: '0 1 auto',
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
              onClick={(e) => {
                e.stopPropagation();
                handlers.onViewResult(record);
              }}
            >
              {name}
            </span>
            {hint && (
              <Tooltip title={tip}>
                <Tag
                  color="orange"
                  tabIndex={0}
                  aria-label={tip}
                  style={{ flex: 'none', margin: 0 }}
                >
                  {t('domainKnowledge.similarity.badge')}
                </Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
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
      title: t('domainKnowledge.quality.column'),
      key: 'quality',
      width: 100,
      render: (_: unknown, record: ManualDocItem) => (
        <DocumentQualityBadge
          supported={record.qualitySupported}
          verdict={record.qualityVerdict}
          reasons={record.qualityReasons}
        />
      ),
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
          compilationPaused={compilationPaused}
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
        <Dropdown menu={{ items: actionItems(record, handlers, t, canWrite, compilationPaused, onToggleCompilation, controlDisabled, kbType !== 'openkb') }} trigger={['click']} placement="bottomRight">
          <Button type="text" icon={<MoreOutlined />} size="small" />
        </Dropdown>
      ),
    },
  ];
};
