import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { Modal, message } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
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
    <Modal
      open={!!target}
      onCancel={() => setTarget(null)}
      onOk={handleDelete}
      okText={t('userManagement.confirmDelete')}
      cancelText={t('common.cancel')}
      okButtonProps={{ danger: true }}
      width={380}
    >
      <div style={{ textAlign: 'center', padding: '8px 0' }}>
        <div style={{ fontSize: 40, marginBottom: 12, color: '#dc2626' }}>
          <ExclamationCircleOutlined />
        </div>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
          {target ? getUserDisplayName(target) || target.username : ''}
        </div>
        <p style={{ color: '#64748b', margin: 0 }}>{t('userManagement.confirmDeleteDesc')}</p>
      </div>
    </Modal>
  );
});

DeleteConfirmModal.displayName = 'DeleteConfirmModal';
export default DeleteConfirmModal;
