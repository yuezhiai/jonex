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
    >
      {version && (
        <>
          <div style={{ display: 'flex', gap: 24, marginBottom: 14, fontSize: 13 }}>
            <div>
              <span style={{ color: '#94a3b8' }}>{t('promptTemplate.versionDetailLabel')}: </span>
              <Tag color="purple">v{version.version}</Tag>
            </div>
            <div>
              <span style={{ color: '#94a3b8' }}>{t('promptTemplate.updateInfo')}: </span>
              {version.updated_at} · {version.updated_by}
            </div>
          </div>
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>
              {t('promptTemplate.updateDescription')}
            </div>
            <div
              style={{ fontSize: 13, color: '#475569', padding: '8px 12px', background: '#f8fafc', borderRadius: 6 }}
            >
              {version.remark || '—'}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>{t('promptTemplate.content')}</div>
            <Paragraph
              copyable
              style={{
                background: '#f8fafc',
                border: '1px solid #e8edf3',
                borderRadius: 8,
                padding: 14,
                fontFamily: "'Courier New', monospace",
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
