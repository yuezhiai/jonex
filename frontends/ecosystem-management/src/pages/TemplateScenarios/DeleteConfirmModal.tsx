import React, { useState, useCallback, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { message } from 'antd';
import {
  deleteTemplateConstraint,
  deleteTemplateObject,
  deleteTemplateRelation,
  deleteTemplateScenario,
} from '../../api/templateScenarios';
import type {
  TemplateConstraint,
  TemplateObject,
  TemplateRelation,
  TemplateScenario,
} from '../../api/templateScenarios';
import {
  getTemplateObjectDisplay,
  getTemplateRelationDisplay,
  getTemplateScenarioDisplay,
} from '../../utils/builtInTemplateDisplay';
import { DangerConfirmModal, getErrorMessage } from '@jonex/shared-lib';

type DeletingItem =
  | { type: 'scene'; item: TemplateScenario }
  | { type: 'object'; item: TemplateObject }
  | { type: 'relation'; item: TemplateRelation }
  | { type: 'constraint'; item: TemplateConstraint };

export interface DeleteConfirmModalHandle {
  open: (item: DeletingItem) => void;
}

interface DeleteConfirmModalProps {
  english: boolean;
  onDeleted: (deleteType: string) => void;
}

const DeleteConfirmModal = forwardRef<DeleteConfirmModalHandle, DeleteConfirmModalProps>(function DeleteConfirmModal(
  { english, onDeleted },
  ref,
) {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [deletingItem, setDeletingItem] = useState<DeletingItem | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useImperativeHandle(
    ref,
    () => ({
      open: (item: DeletingItem) => {
        setDeletingItem(item);
        setModalOpen(true);
      },
    }),
    [],
  );

  const handleConfirm = useCallback(async () => {
    if (!deletingItem) return;

    setSubmitting(true);
    try {
      if (deletingItem.type === 'scene') {
        await deleteTemplateScenario(deletingItem.item.id);
        message.success(t('templateScenarios.sceneDeleted'));
      }

      if (deletingItem.type === 'object') {
        await deleteTemplateObject(deletingItem.item.id);
        message.success(t('templateScenarios.objectDeleted'));
      }

      if (deletingItem.type === 'relation') {
        await deleteTemplateRelation(deletingItem.item.id);
        message.success(t('templateScenarios.relationDeleted'));
      }

      if (deletingItem.type === 'constraint') {
        await deleteTemplateConstraint(deletingItem.item.id);
        message.success(t('templateScenarios.constraintDeleted'));
      }

      setModalOpen(false);
      const deleteType = deletingItem.type;
      setDeletingItem(null);
      onDeleted(deleteType);
    } catch (error) {
      message.error(getErrorMessage(error, t('templateScenarios.deleteFailed')));
    } finally {
      setSubmitting(false);
    }
  }, [deletingItem, onDeleted, t]);


  const deleteName =
    deletingItem?.type === 'scene'
      ? getTemplateScenarioDisplay(deletingItem.item, english, t).name
      : deletingItem?.type === 'object'
        ? getTemplateObjectDisplay(deletingItem.item, english).name
        : deletingItem?.type === 'relation'
          ? getTemplateRelationDisplay(deletingItem.item, english).name
          : deletingItem?.item.name;

  return (
    <DangerConfirmModal
      open={modalOpen}
      title={t('templateScenarios.deleteConfirmContent', { name: deleteName })}
      okText={t('templateScenarios.confirmDelete')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      onOk={handleConfirm}
      onCancel={() => setModalOpen(false)}
    />
  );
});

export default DeleteConfirmModal;
