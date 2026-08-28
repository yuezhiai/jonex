import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { Form, Modal, Input, Select, message } from 'antd';
import { useTranslation } from 'react-i18next';
import type { TemplateDomain } from '../../api/templateDomains';
import { createDomain, updateDomain } from '../../api/templateDomains';

export interface DomainFormModalHandle {
  open: (domain?: TemplateDomain) => void;
}

interface DomainFormModalProps {
  onSuccess?: () => void;
}

const DomainFormModal = forwardRef<DomainFormModalHandle, DomainFormModalProps>(({ onSuccess }, ref) => {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [editingDomain, setEditingDomain] = useState<TemplateDomain | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  // 对外暴露方法
  useImperativeHandle(
    ref,
    () => ({
      open(domain?: TemplateDomain) {
        if (domain) {
          setEditingDomain(domain);
          form.setFieldsValue({
            name: domain.name,
            name_en: domain.name_en || '',
            description: domain.description || '',
            status: domain.status,
          });
        } else {
          setEditingDomain(null);
          form.setFieldsValue({ name: '', name_en: '', description: '', status: 'active' });
        }
        setModalOpen(true);
      },
    }),
    [form],
  );

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      if (editingDomain) {
        await updateDomain(editingDomain.id, {
          name: values.name.trim(),
          name_en: values.name_en?.trim() || undefined,
          description: values.description?.trim() || undefined,
          status: values.status,
        });
        message.success(t('templateDomains.domainUpdated'));
      } else {
        await createDomain({
          name: values.name.trim(),
          name_en: values.name_en?.trim() || undefined,
          description: values.description?.trim() || undefined,
          status: values.status,
        });
        message.success(t('templateDomains.domainCreated'));
      }
      setModalOpen(false);
      onSuccess?.();
    } catch (e: any) {
      if (e && typeof e === 'object' && 'errorFields' in e) return;
      message.error(e?.message || t('common.operationFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={editingDomain ? t('templateDomains.editTitle') : t('templateDomains.createTitle')}
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      width={520}
      destroyOnClose
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        <Form.Item
          name="name"
          label={t('templateDomains.nameLabel')}
          rules={[{ required: true, message: t('templateDomains.nameWarning') }]}
        >
          <Input placeholder={t('templateDomains.namePlaceholder')} />
        </Form.Item>
        <Form.Item name="name_en" label={t('templateDomains.nameEnLabel')}>
          <Input placeholder={t('templateDomains.nameEnPlaceholder')} />
        </Form.Item>
        <Form.Item name="description" label={t('common.description')}>
          <Input.TextArea placeholder={t('templateDomains.descPlaceholder')} rows={3} />
        </Form.Item>
        <Form.Item name="status" label={t('common.status')}>
          <Select
            style={{ width: '100%' }}
            options={[
              { label: t('status.active'), value: 'active' },
              { label: t('status.inactive'), value: 'inactive' },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
});

DomainFormModal.displayName = 'DomainFormModal';

export default DomainFormModal;
