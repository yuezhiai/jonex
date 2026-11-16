import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { message } from 'antd';
import { useTranslation } from 'react-i18next';
import { DangerConfirmModal } from '@jonex/shared-lib';
import { deleteUser, type UserItem } from '../../api/users';

export interface DeleteConfirmModalHandle {
  open: (user: UserItem) => void;
  close: () => void;
}

interface Props {
  getUserDisplayName: (user: UserItem) => string | null;
  onSaved: () => Promise<void>;
}

const DeleteConfirmModal = forwardRef<DeleteConfirmModalHandle, Props>(({ getUserDisplayName, onSaved }, ref) => {
  const { t } = useTranslation();
  const [target, setTarget] = useState<UserItem | null>(null);

  useImperativeHandle(
    ref,
    () => ({
      open(user) {
        setTarget(user);
      },
      close() {
        setTarget(null);
      },
    }),
    [],
  );

  const handleDelete = async () => {
    if (!target) return;
    try {
      // [jonex] 带目标用户所属租户，支持平台管理员跨租户删除（理由同 ToggleStatusModal）
      await deleteUser(target.id, target.tenant_id);
      message.success(t('common.deleteSuccess'));
      setTarget(null);
      await onSaved();
    } catch (err: any) {
      message.error(err?.message || t('userManagement.deleteFailed'));
    }
  };

  return (
    <DangerConfirmModal
      open={!!target}
      title={target ? getUserDisplayName(target) || target.username : ''}
      description={t('userManagement.confirmDeleteDesc')}
      okText={t('userManagement.confirmDelete')}
      cancelText={t('common.cancel')}
      onOk={handleDelete}
      onCancel={() => setTarget(null)}
    />
  );
});

DeleteConfirmModal.displayName = 'DeleteConfirmModal';
export default DeleteConfirmModal;
