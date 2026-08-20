import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Select, Spin, message } from 'antd';
import { listRoleUsers, setRoleUsers, type RoleItem } from '../../api/roles';
import { listAllUsers, listUsers, type UserItem } from '../../api/users';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';

export interface RoleUserAssignModalRef {
  open: (role: RoleItem) => void;
}

interface Props {
  onSaved: () => void;
}

const RoleUserAssignModal = forwardRef<RoleUserAssignModalRef, Props>(({ onSaved }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<RoleItem | null>(null);
  const [users, setUsers] = useState<UserItem[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  useImperativeHandle(ref, () => ({
    open: async (r: RoleItem) => {
      setRole(r);
      setOpen(true);
      setLoading(true);
      try {
        // 权限分支：平台管理员跨租户全量；其余本租户（角色是租户实体，分配用户仅限本租户）
        const cachedUser = readCachedUser<ShellUser>();
        const [all, assigned] = await Promise.all([
          isPlatformAdmin(cachedUser) ? listAllUsers() : listUsers(1, 100),
          listRoleUsers(r.id),
        ]);
        setUsers(all.items);
        setSelected(assigned);
      } catch {
        message.error(t('rolePermission.loadUsersFailed'));
      } finally {
        setLoading(false);
      }
    },
  }));

  const handleSave = async () => {
    if (!role) return;
    setSaving(true);
    try {
      await setRoleUsers(role.id, selected);
      message.success(t('rolePermission.usersUpdated'));
      setOpen(false);
      onSaved();
    } catch {
      message.error(t('rolePermission.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setOpen(false);
  };

  return (
    <Modal
      title={t('rolePermission.assignUsersTitle', { name: role?.name })}
      open={open}
      onCancel={handleCancel}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      width={560}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : (
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          placeholder={t('rolePermission.assignUsers')}
          value={selected}
          onChange={(v) => setSelected(v as number[])}
          optionFilterProp="label"
          options={users.map((u) => ({
            value: u.id,
            label: u.display_name || u.username,
          }))}
        />
      )}
    </Modal>
  );
});

RoleUserAssignModal.displayName = 'RoleUserAssignModal';
export default RoleUserAssignModal;
