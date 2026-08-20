import React, { useState, useEffect, forwardRef, useImperativeHandle } from 'react';
import { Form, Input, Modal, Select, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  createUser,
  updateUser,
  getUserRoles,
  setUserRoles,
  type UserItem,
  type UserCreatePayload,
} from '../../api/users';
import { listRoles, type RoleItem } from '../../api/roles';
import type { TenantItem } from '../../api/tenants';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import { tenantDisplay } from '../../utils/tenantDisplay';

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
  const [roleOptions, setRoleOptions] = useState<RoleItem[]>([]);
  const [roleLoading, setRoleLoading] = useState(false);
  const [form] = Form.useForm();
  // 目标租户：新建 = 租户下拉（可切换）；编辑 = 隐藏字段（固定为用户所属租户）
  const targetTenantId = Form.useWatch('target_tenant_id', form);

  // 目标租户变化 → 重拉该租户角色列表（新建切换租户 / 编辑初次赋值均触发）
  useEffect(() => {
    if (!open) return;
    void loadRoles(targetTenantId ?? '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetTenantId, open]);

  const loadRoles = async (tenantId: string) => {
    if (!tenantId) {
      // 未选租户：角色下拉空 + 禁用
      setRoleOptions([]);
      return;
    }
    // 拉目标租户角色列表（RBAC roles 表）；
    // 与 role-permission 页同款过滤：非平台管理员不可见系统角色（is_system=1）
    const cachedUser = readCachedUser<ShellUser>();
    const isPlatformAdminFlag = isPlatformAdmin(cachedUser);
    setRoleLoading(true);
    try {
      const roleList = await listRoles(1, 100, tenantId);
      const visibleRoles = isPlatformAdminFlag
        ? roleList.items
        : roleList.items.filter((r) => r.is_system !== 1);

      if (editing) {
        // 编辑：当前绑定角色若被过滤（系统角色），保留在选项中防误改丢失绑定
        let boundRoleIds: number[] = [];
        try {
          boundRoleIds = await getUserRoles(editing.id, tenantId);
        } catch {
          boundRoleIds = [];
        }
        if (boundRoleIds[0] != null && !visibleRoles.some((r) => r.id === boundRoleIds[0])) {
          const bound = roleList.items.find((r) => r.id === boundRoleIds[0]);
          if (bound) visibleRoles.push(bound);
        }
        setRoleOptions(visibleRoles);
        form.setFieldValue('role_id', boundRoleIds[0]);
      } else {
        setRoleOptions(visibleRoles);
        // 切换租户清空已选角色，防止跨租户 role_id 残留
        form.setFieldValue('role_id', undefined);
      }
    } catch {
      setRoleOptions([]);
    } finally {
      setRoleLoading(false);
    }
  };

  useImperativeHandle(
    ref,
    () => ({
      open(user) {
        const cachedUser = readCachedUser<ShellUser>();
        setEditing(user ?? null);
        if (user) {
          form.setFieldsValue({
            username: user.username,
            password: '',
            display_name: user.display_name || '',
            email: user.email || '',
            role_id: undefined,
            new_password: '',
            // 编辑：目标租户固定为用户所属租户（隐藏字段驱动角色加载）
            target_tenant_id: user.tenant_id,
          });
        } else {
          form.setFieldsValue({
            username: '',
            password: '',
            display_name: '',
            email: '',
            role_id: undefined,
            new_password: '',
            // 新建：默认操作者租户，可切换（切换触发角色重载）
            target_tenant_id: cachedUser?.tenantId ?? undefined,
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
        await updateUser(
          editing.id,
          { display_name: values.display_name, email: values.email },
          editing.tenant_id,
        );
        // 角色绑定走 RBAC user_roles（delete-then-insert），按用户所属租户
        await setUserRoles(editing.id, values.role_id != null ? [values.role_id] : [], editing.tenant_id);
        message.success(t('userManagement.updated'));
      } else {
        const payload: UserCreatePayload = {
          username: values.username,
          password: values.password,
          display_name: values.display_name,
          email: values.email,
          target_tenant_id: values.target_tenant_id,
          role_id: values.role_id,
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
        {/* 编辑模式：隐藏目标租户字段（仅驱动角色加载，不展示选择器） */}
        {editing && (
          <Form.Item name="target_tenant_id" hidden>
            <Input />
          </Form.Item>
        )}
        {/* 新建模式：先选租户，再按租户加载角色 */}
        {!editing && (
          <Form.Item
            name="target_tenant_id"
            label={t('userManagement.tenant')}
            rules={[{ required: true, message: t('userManagement.requiredTenant') }]}
          >
            <Select
              options={tenants.map((tenant) => ({
                label: tenantDisplay(tenant, t).name,
                value: tenant.id,
              }))}
            />
          </Form.Item>
        )}
        <Form.Item
          name="role_id"
          label={t('userManagement.role')}
          rules={[{ required: true, message: t('userManagement.requiredRole') }]}
        >
          <Select
            placeholder={targetTenantId ? t('userManagement.requiredRole') : t('userManagement.requiredTenant')}
            loading={roleLoading}
            disabled={!targetTenantId}
            options={roleOptions.map((role) => ({
              label: role.name,
              value: role.id,
            }))}
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
