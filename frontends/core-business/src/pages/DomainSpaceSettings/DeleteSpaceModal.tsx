import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { message } from 'antd';
import { deleteSpace } from '../../api/domainSpace';
import { useStore } from '../../store';
import { emitSpacesInvalidated, emitSpaceChanged } from '@jonex/shell-sdk';
import { DangerConfirmModal } from '@jonex/shared-lib';

export interface DeleteSpaceModalHandle {
  open: (spaceName: string) => void;
}

interface DeleteSpaceModalProps {
  spaceId: string;
  onDeleted: () => void;
}

const DeleteSpaceModal = forwardRef<DeleteSpaceModalHandle, DeleteSpaceModalProps>(function DeleteSpaceModal(
  { spaceId, onDeleted },
  ref,
) {
  const { t } = useTranslation();
  const { global } = useStore();

  const [open, setOpen] = useState(false);
  const [spaceName, setSpaceName] = useState('');
  const [deleting, setDeleting] = useState(false);

  useImperativeHandle(
    ref,
    () => ({
      open: (name: string) => {
        setSpaceName(name);
        setOpen(true);
      },
    }),
    [],
  );

  const handleDelete = async () => {
    setDeleting(true);
    try {
      const wasCurrent = global.currentSpaceId === spaceId;
      await deleteSpace(spaceId);
      message.success(t('common.deleteSuccess'));
      setOpen(false);
      await global.refreshSpaces();
      if (wasCurrent) emitSpaceChanged(global.currentSpaceId);
      emitSpacesInvalidated();
      onDeleted();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <DangerConfirmModal
      open={open}
      title={t('domainSpace.confirmDeleteMessage', { name: spaceName })}
      description={t('domainSpace.deleteWarning')}
      okText={t('common.okText')}
      cancelText={t('common.cancel')}
      confirmLoading={deleting}
      onOk={handleDelete}
      onCancel={() => setOpen(false)}
    />
  );
});

export default DeleteSpaceModal;
