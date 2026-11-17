import React from 'react';
import { useTranslation } from 'react-i18next';
import { DangerConfirmModal } from '@jonex/shared-lib';
import type { DomainKnowledgeItem } from '@/types/domainKnowledge';

interface DeleteConfirmModalProps {
  open: boolean;
  deletingKb: DomainKnowledgeItem | null;
  submitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function DeleteConfirmModal({
  open,
  deletingKb,
  submitting,
  onCancel,
  onConfirm,
}: DeleteConfirmModalProps) {
  const { t } = useTranslation();

  return (
    <DangerConfirmModal
      open={open}
      title={t('domainKnowledge.confirmDeleteContent', { name: deletingKb?.name })}
      description={t('common.deleteWarning')}
      okText={t('common.confirmDelete')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onOk={onConfirm}
      onCancel={onCancel}
    />
  );
}
