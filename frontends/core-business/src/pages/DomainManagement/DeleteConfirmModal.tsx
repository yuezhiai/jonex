import React from 'react';
import { useTranslation } from 'react-i18next';
import { DangerConfirmModal } from '@jonex/shared-lib';
import type { DomainServiceItem } from '../../types/domainService';

interface DeleteConfirmModalProps {
  open: boolean;
  deleteTarget: DomainServiceItem | null;
  submitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function DeleteConfirmModal({
  open,
  deleteTarget,
  submitting,
  onCancel,
  onConfirm,
}: DeleteConfirmModalProps) {
  const { t } = useTranslation();

  return (
    <DangerConfirmModal
      open={open}
      title={t('common.confirmDeleteContent', { name: deleteTarget?.name || '' })}
      description={t('common.deleteWarning')}
      okText={t('common.okText')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onOk={onConfirm}
      onCancel={onCancel}
    />
  );
}
