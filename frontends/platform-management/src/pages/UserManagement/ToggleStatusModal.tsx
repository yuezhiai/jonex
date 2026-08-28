import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { Modal, message } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { updateUser, type UserItem } from '../../api/users';

export interface ToggleStatusModalHandle {
  open: (user: UserItem) => void;
  close: () => void;
}

interface Props {
  getUserDisplayName: (user: UserItem) => string | null;
  onSaved: () => Promise<void>;
}

const ToggleStatusModal = forwardRef<ToggleStatusModalHandle, Props>(({ getUserDisplayName, onSaved }, ref) => {
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

  const handleToggle = async () => {
    if (!target) return;
    const newStatus = target.status === 1 ? 0 : 1;
    try {
      await updateUser(target.id, { status: newStatus });
      message.success(newStatus === 1 ? t('userManagement.enabled') : t('userManagement.disabled'));
      setTarget(null);
      await onSaved();
    } catch (err: any) {
      message.error(err?.message || t('userManagement.operationFailed'));
    }
  };

  return (
    <Modal
      open={!!target}
      onCancel={() => setTarget(null)}
      onOk={handleToggle}
      okText={target?.status === 1 ? t('userManagement.confirmDisable') : t('userManagement.confirmEnable')}
      cancelText={t('common.cancel')}
      width={380}
    >
      <div style={{ textAlign: 'center', padding: '8px 0' }}>
        <div style={{ fontSize: 40, marginBottom: 12, color: target?.status === 1 ? '#f59e0b' : '#10b981' }}>
          <ExclamationCircleOutlined />
        </div>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
          {target ? getUserDisplayName(target) || target.username : ''}
        </div>
        <p style={{ color: '#64748b', margin: 0 }}>
          {target?.status === 1 ? t('userManagement.confirmDisableDesc') : t('userManagement.confirmEnableDesc')}
        </p>
      </div>
    </Modal>
  );
});

ToggleStatusModal.displayName = 'ToggleStatusModal';
export default ToggleStatusModal;
