import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { Modal, message } from 'antd';
import { ExclamationCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { TemplateDomain } from '../../api/templateDomains';
import { deleteDomain } from '../../api/templateDomains';

export interface DomainDeleteModalHandle {
  open: (domain: TemplateDomain) => void;
}

interface DomainDeleteModalProps {
  onSuccess?: () => void;
}

const DomainDeleteModal = forwardRef<DomainDeleteModalHandle, DomainDeleteModalProps>(({ onSuccess }, ref) => {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [deletingDomain, setDeletingDomain] = useState<TemplateDomain | null>(null);

  // 对外暴露方法
  useImperativeHandle(
    ref,
    () => ({
      open(domain: TemplateDomain) {
        setDeletingDomain(domain);
        setModalOpen(true);
      },
    }),
    [],
  );

  const handleDelete = async () => {
    if (!deletingDomain) return;
    try {
      await deleteDomain(deletingDomain.id);
      message.success(t('templateDomains.domainDeleted'));
      setModalOpen(false);
      setDeletingDomain(null);
      onSuccess?.();
    } catch (err: any) {
      message.error(err?.message || t('common.deleteFailed'));
    }
  };

  return (
    <Modal
      title={null}
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      onOk={handleDelete}
      okText={t('common.confirmDelete')}
      cancelText={t('common.cancel')}
      okButtonProps={{ danger: true }}
      width={400}
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
          <ExclamationCircleOutlined />
        </div>
        <div>
          <strong style={{ fontSize: 16 }}>{deletingDomain?.name}</strong>
          <p style={{ fontSize: 14, color: '#64748b', margin: '4px 0 0' }}>
            {t('templateDomains.confirmDeleteContent')}
          </p>
        </div>
      </div>
    </Modal>
  );
});

DomainDeleteModal.displayName = 'DomainDeleteModal';

export default DomainDeleteModal;
