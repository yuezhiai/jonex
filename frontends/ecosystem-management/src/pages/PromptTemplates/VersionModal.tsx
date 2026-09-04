import React, { useEffect, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Table, Button, Tag, Typography, message, Popconfirm } from 'antd';
import { EyeOutlined, RollbackOutlined, BranchesOutlined } from '@ant-design/icons';
import { listVersions, rollbackVersion, type VersionItem, type PromptTemplateItem } from '../../api/promptTemplates';
import { promptTemplateVersionsDisplay } from '../../utils/systemPromptTemplateDisplay';

interface VersionModalProps {
  open: boolean;
  template: PromptTemplateItem | null;
  onClose: () => void;
  onRollback: () => void; // refresh parent after rollback
  onViewDetail: (version: VersionItem) => void;
  domainSpaceId?: string | null;
}

const VersionModal: React.FC<VersionModalProps> = ({
  open,
  template,
  onClose,
  onRollback,
  onViewDetail,
  domainSpaceId,
}) => {
  const [versions, setVersions] = useState<VersionItem[]>([]);
  const [currentVersion, setCurrentVersion] = useState('');
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);

  const loadVersions = useCallback(async () => {
    if (!template) return;
    setLoading(true);
    try {
      const result = await listVersions(template.id, domainSpaceId ?? undefined);
      setVersions(promptTemplateVersionsDisplay(template, result.items, t));
      setCurrentVersion(result.current_version);
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('promptTemplate.loadHistoryError'));
    } finally {
      setLoading(false);
    }
  }, [template, t, domainSpaceId]);

  useEffect(() => {
    if (open && template) loadVersions();
  }, [open, template, loadVersions]);

  const handleRollback = async (targetVersion: string) => {
    if (!template) return;
    try {
      await rollbackVersion(template.id, targetVersion, domainSpaceId ?? undefined);
      message.success(t('promptTemplate.rollbackSuccess', { version: targetVersion }));
      await loadVersions();
      onRollback();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('promptTemplate.rollbackFailed'));
    }
  };

  const columns = [
    {
      title: t('promptTemplate.versionColumn'),
      dataIndex: 'version',
      width: 100,
      render: (v: string, _: VersionItem, idx: number) => (
        <span>
          <span style={{ fontWeight: 600, color: '#7c3aed' }}>v{v}</span>
          {idx === 0 && (
            <Tag color="green" style={{ marginLeft: 6, fontSize: 10 }}>
              {t('promptTemplate.currentTag')}
            </Tag>
          )}
        </span>
      ),
    },
    {
      title: t('promptTemplate.contentPreview'),
      dataIndex: 'content',
      width: 320,
      render: (content: string) => (
        <Typography.Paragraph
          type="secondary"
          ellipsis={{ rows: 2, tooltip: content || undefined }}
          style={{ margin: 0 }}
        >
          {content || ''}
        </Typography.Paragraph>
      ),
    },
    {
      title: t('promptTemplate.updateDescription'),
      dataIndex: 'remark',
      width: 90,
      ellipsis: true,
      render: (remark: string) => <span>{remark || '—'}</span>,
    },
    { title: t('promptTemplate.updatedBy'), dataIndex: 'updated_by', width: 120 },
    { title: t('promptTemplate.updatedAt'), dataIndex: 'updated_at', width: 150 },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 120,
      render: (_: unknown, record: VersionItem, idx: number) => (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          <Button size="small" icon={<EyeOutlined />} onClick={() => onViewDetail(record)}>
            {t('promptTemplate.viewBtn')}
          </Button>
          {idx > 0 && (
            <Popconfirm
              title={t('promptTemplate.rollbackConfirm', { version: record.version })}
              description={t('promptTemplate.rollbackConfirmDesc')}
              onConfirm={() => handleRollback(record.version)}
              okText={t('promptTemplate.rollbackBtn')}
              cancelText={t('common.cancel')}
            >
              <Button size="small" icon={<RollbackOutlined />} style={{ color: '#f59e0b' }}>
                {t('promptTemplate.rollback')}
              </Button>
            </Popconfirm>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <Modal
        title={
          <span>
            <BranchesOutlined style={{ color: '#7c3aed', marginRight: 8 }} />
            {t('promptTemplate.version')} · {template?.name}
          </span>
        }
        open={open}
        onCancel={onClose}
        footer={<Button onClick={onClose}>{t('common.close')}</Button>}
        width={1080}
      >
        <div style={{ marginBottom: 16, fontSize: 13, color: '#475569' }}>
          <span>
            {t('promptTemplate.currentVersion')}: <Tag color="purple">v{currentVersion}</Tag>
          </span>
          <span style={{ marginLeft: 16 }}>
            {t('promptTemplate.versionCountLabel')} <strong>{versions.length}</strong>{' '}
            {t('promptTemplate.versionCountUnit')}
          </span>
        </div>
        <Table
          dataSource={versions}
          columns={columns}
          rowKey="version"
          loading={loading}
          pagination={false}
          size="small"
        />
      </Modal>
    </>
  );
};

export default React.memo(VersionModal);
