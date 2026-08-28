import React from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Input, Button, message } from 'antd';
import { CopyOutlined } from '@ant-design/icons';
import copy from 'copy-to-clipboard';

interface Props {
  open: boolean;
  title: string;
  desc: string;
  content: string;
  onClose: () => void;
}

export default function PromptViewModal({ open, title, desc, content, onClose }: Props) {
  const { t } = useTranslation();
  const handleCopy = async () => {
    const ok = await copy(content);
    if (ok) message.success(t('compile.promptModal.copySuccess'));
    else message.error(t('compile.promptModal.copyFailed'));
  };
  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      width={640}
      footer={[
        <Button key="copy" icon={<CopyOutlined />} onClick={handleCopy}>
          {t('common.copy')}
        </Button>,
        <Button key="close" type="primary" onClick={onClose}>
          {t('common.close')}
        </Button>,
      ]}
    >
      <p style={{ fontSize: 13, color: '#64748b', marginBottom: 12 }}>{desc}</p>
      <Input.TextArea value={content} readOnly rows={14} style={{ fontSize: 13 }} />
    </Modal>
  );
}
