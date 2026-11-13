import React from 'react';
import { useTranslation } from 'react-i18next';
import { message } from 'antd';
import { DangerConfirmModal } from '@jonex/shared-lib';
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
    <DangerConfirmModal
      open={!!deletingId}
      title={t('promptTemplate.deleteConfirm')}
      description={t('promptTemplate.deleteConfirmDesc')}
      okText={t('promptTemplate.confirmDeleteBtn')}
      cancelText={t('common.cancel')}
      onOk={handleDelete}
      onCancel={onClose}
    />
  );
}
