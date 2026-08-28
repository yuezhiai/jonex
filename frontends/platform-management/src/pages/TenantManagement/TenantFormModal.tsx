import React, { forwardRef, useImperativeHandle, useState } from 'react';
import { Form, Input, Modal, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  createTenant,
  updateTenant,
  type TenantItem,
  type TenantCreatePayload,
  type TenantUpdatePayload,
} from '../../api/tenants';

export interface TenantFormModalRef {
  openCreate: () => void;
  openEdit: (item: TenantItem) => void;
}

const TenantFormModal = forwardRef<TenantFormModalRef, { onSaved: () => void }>(function TenantFormModal(
  { onSaved },
  ref,
) {
  const { t } = useTranslation();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<TenantItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();

  useImperativeHandle(ref, () => ({
    openCreate() {
      setEditing(null);
      form.setFieldsValue({ id: '', name: '', description: '' });
      setModalOpen(true);
    },
    openEdit(item: TenantItem) {
      setEditing(item);
      form.setFieldsValue({
        id: item.id,
        name: item.name,
        description: item.description || '',
      });
      setModalOpen(true);
    },
  }));

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      // 租户 ID 与名称去除首尾空格后提交
      const tenantId = (values.id ?? '').trim();
      const tenantName = (values.name ?? '').trim();
      setSubmitting(true);
      if (editing) {
        const payload: TenantUpdatePayload = {
          name: tenantName,
          description: values.description,
        };
        await updateTenant(editing.id, payload);
        message.success(t('tenantManagement.updated'));
      } else {
        const payload: TenantCreatePayload = {
          id: tenantId,
          name: tenantName,
          description: values.description,
        };
        await createTenant(payload);
        message.success(t('tenantManagement.created'));
      }
      setModalOpen(false);
      onSaved();
    } catch (e: unknown) {
      if (e && typeof e === 'object' && 'errorFields' in e) return;
      message.error(e instanceof Error ? e.message : t('tenantManagement.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={editing ? t('tenantManagement.editTenant') : t('tenantManagement.createTenant')}
      open={modalOpen}
      onCancel={() => setModalOpen(false)}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={480}
      destroyOnClose
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        {!editing && (
          <Form.Item
            name="id"
            label={t('tenantManagement.tenantId')}
            rules={[{ required: true, whitespace: true, message: t('tenantManagement.requiredTenantId') }]}
            getValueFromEvent={(e) => e.target.value.replace(/\s+/g, '')}
          >
            <Input placeholder={t('tenantManagement.placeholderTenantId')} />
          </Form.Item>
        )}
        <Form.Item
          name="name"
          label={t('tenantManagement.name')}
          rules={[{ required: true, whitespace: true, message: t('tenantManagement.requiredName') }]}
          getValueFromEvent={(e) => e.target.value.replace(/\s+/g, '')}
        >
          <Input placeholder={t('tenantManagement.placeholderName')} />
        </Form.Item>
        <Form.Item name="description" label={t('tenantManagement.description')}>
          <Input placeholder={t('tenantManagement.placeholderDescription')} />
        </Form.Item>
      </Form>
    </Modal>
  );
});

export default TenantFormModal;
