import React from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Button, Tag, message, Typography } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import copy from 'copy-to-clipboard';
import type { VersionItem } from '../../api/promptTemplates';

const { Paragraph } = Typography;

interface VersionDetailModalProps {
  open: boolean;
  version: VersionItem | null;
  onClose: () => void;
}

export default function VersionDetailModal({ open, version, onClose }: VersionDetailModalProps) {
  const { t } = useTranslation();

  const handleCopyContent = async () => {
    if (!version) return;
    const ok = await copy(version.content);
    if (ok) message.success(t('promptTemplate.copySuccess'));
    else message.error(t('promptTemplate.copyFailed'));
  };

  return (
    <Modal
      title={t('promptTemplate.versionDetailTitle')}
      open={open}
      onCancel={onClose}
      footer={[
        <Button key="copy" icon={<CopyOutlined />} onClick={handleCopyContent}>
          {t('promptTemplate.copyContent')}
        </Button>,
        <Button key="close" onClick={onClose}>
          {t('common.close')}
        </Button>,
      ]}
      width={700}
      // 从「版本历史」弹窗里打开，必须压在上面：antd 的顶层容器 z-index 是平的
      // （zIndexPopupBase + 100），谁在上面只由 portal 在 body 里的先后决定。
      // 分层见 VersionHistoryModal 的注释；1200 仍低于 antd message / notification（2110）。
      zIndex={1200}
    >
      {version && (
        <>
          <div style={{ display: 'flex', gap: 24, marginBottom: 14, fontSize: 13 }}>
            <div>
              <span style={{ color: 'var(--yx-text-muted)' }}>{t('promptTemplate.versionDetailLabel')}: </span>
              <Tag color="purple">v{version.version}</Tag>
            </div>
            <div>
              <span style={{ color: 'var(--yx-text-muted)' }}>{t('promptTemplate.updateInfo')}: </span>
              {version.updated_at} · {version.updated_by}
            </div>
          </div>
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginBottom: 4 }}>
              {t('promptTemplate.updateDescription')}
            </div>
            <div
              style={{ fontSize: 13, color: '#475569', padding: '8px 12px', background: 'var(--yx-bg-subtle)', borderRadius: 6 }}
            >
              {version.remark || '—'}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginBottom: 4 }}>{t('promptTemplate.content')}</div>
            <Paragraph
              copyable
              style={{
                background: 'var(--yx-bg-subtle)',
                border: '1px solid #e8edf3',
                borderRadius: 8,
                padding: 14,
                fontSize: 12,
                color: '#475569',
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                maxHeight: 320,
                overflow: 'auto',
              }}
            >
              {version.content}
            </Paragraph>
          </div>
        </>
      )}
    </Modal>
  );
}
