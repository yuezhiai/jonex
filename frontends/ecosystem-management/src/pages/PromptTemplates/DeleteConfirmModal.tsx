import React from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, message } from 'antd';
import { deletePromptTemplate } from '../../api/promptTemplates';

interface DeleteConfirmModalProps {
  deletingId: string | null;
  domainSpaceId?: string | null;
  onClose: () => void;
  onDeleted: () => void;
}

export default function DeleteConfirmModal({ deletingId, domainSpaceId, onClose, onDeleted }: DeleteConfirmModalProps) {
  const { t } = useTranslation();

  const handleDelete = async () => {
    if (!deletingId) return;
    try {
      await deletePromptTemplate(deletingId, domainSpaceId ?? undefined);
      message.success(t('promptTemplate.deleteSuccess'));
      onClose();
      onDeleted();
    } catch (err: any) {
      message.error(err?.message || t('common.operationFailed'));
    }
  };

  return (
    <Modal
      title={t('promptTemplate.delete')}
      open={!!deletingId}
      onOk={handleDelete}
      onCancel={onClose}
      okText={t('promptTemplate.confirmDeleteBtn')}
      cancelText={t('common.cancel')}
      okButtonProps={{ danger: true }}
    >
      <div style={{ textAlign: 'center', padding: '12px 0' }}>
        <div
          style={{
            width: 48,
            height: 48,
            borderRadius: 12,
            background: '#fef2f2',
            color: '#dc2626',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 22,
            margin: '0 auto 12px',
          }}
        >
          🗑️
        </div>
        <strong>{t('promptTemplate.deleteConfirm')}</strong>
        <p style={{ color: '#64748b', margin: '4px 0 0', fontSize: 13 }}>{t('promptTemplate.deleteConfirmDesc')}</p>
      </div>
    </Modal>
  );
}
