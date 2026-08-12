import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { Form, Input, Modal, Select, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { createUser, updateUser, type UserItem, type UserCreatePayload } from '../../api/users';
import type { TenantItem } from '../../api/tenants';

export interface UserFormModalHandle {
  open: (user?: UserItem) => void;
  close: () => void;
}

interface Props {
  tenants: (TenantItem & { userCount: number })[];
  onSaved: () => Promise<void>;
}

const UserFormModal = forwardRef<UserFormModalHandle, Props>(({ tenants, onSaved }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<UserItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm();

  useImperativeHandle(
    ref,
    () => ({
      open(user) {
        if (user) {
          setEditing(user);
          form.setFieldsValue({
            username: user.username,
            password: '',
            display_name: user.display_name || '',
            email: user.email || '',
            role: user.role,
            new_password: '',
          });
        } else {
          setEditing(null);
          form.setFieldsValue({
            username: '',
            password: '',
            display_name: '',
            email: '',
            role: 'user',
            new_password: '',
          });
        }
        setOpen(true);
      },
      close() {
        setOpen(false);
      },
    }),
    [form],
  );

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      if (editing) {
        await updateUser(editing.id, { display_name: values.display_name, email: values.email, role: values.role });
        message.success(t('userManagement.updated'));
      } else {
        const payload: UserCreatePayload = {
          username: values.username,
          password: values.password,
          display_name: values.display_name,
          email: values.email,
          role: values.role,
        };
        await createUser(payload);
        message.success(t('userManagement.created'));
      }
      setOpen(false);
      await onSaved();
    } catch (e: unknown) {
      if (e && typeof e === 'object' && 'errorFields' in e) return;
      message.error(e instanceof Error ? e.message : t('userManagement.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={editing ? t('userManagement.editUser') : t('userManagement.createUser')}
      open={open}
      onCancel={() => setOpen(false)}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      width={520}
      destroyOnClose
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }} autoComplete="off">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <Form.Item
            name="username"
            label={t('userManagement.username')}
            rules={editing ? [] : [{ required: true, message: t('userManagement.requiredUsername') }]}
          >
            <Input placeholder={t('userManagement.placeholderUsername')} disabled={!!editing} autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="display_name"
            label={t('userManagement.displayName')}
            rules={[{ required: true, message: t('userManagement.requiredDisplayName') }]}
          >
            <Input placeholder={t('userManagement.placeholderDisplayName')} />
          </Form.Item>
        </div>
        <Form.Item
          name="email"
          label={t('userManagement.email')}
          rules={[{ required: true, message: t('userManagement.requiredEmail'), type: 'email' }]}
        >
          <Input type="email" placeholder={t('userManagement.placeholderEmail')} autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="role"
          label={t('userManagement.role')}
          rules={[{ required: true, message: t('userManagement.requiredRole') }]}
        >
          <Select
            options={[
              { label: t('auth.systemAdmin'), value: 'admin' },
              { label: t('userManagement.roleUser'), value: 'user' },
            ]}
          />
        </Form.Item>
        {!editing && (
          <Form.Item
            name="password"
            label={t('auth.password')}
            rules={[{ required: true, message: t('userManagement.requiredPassword') }]}
          >
            <Input.Password placeholder={t('userManagement.placeholderPassword')} autoComplete="new-password" />
          </Form.Item>
        )}
        {editing && (
          <Form.Item name="new_password" label={t('userManagement.newPassword')}>
            <Input.Password placeholder={t('userManagement.placeholderNewPassword')} autoComplete="new-password" />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
});

UserFormModal.displayName = 'UserFormModal';
export default UserFormModal;
