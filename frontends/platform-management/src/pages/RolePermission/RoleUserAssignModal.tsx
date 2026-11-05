import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Select, Spin, message } from 'antd';
import { listRoleUsers, setRoleUsers, type RoleItem } from '../../api/roles';
import { listAllTenantUsers, listAllUsers, type UserItem } from '../../api/users';
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
  // 加载失败时禁止保存：selected 此时是空/上一次角色的名单，
  // 直接提交会把这行名单整体覆盖到当前角色上（清空或串角色写入）。
  const [loadFailed, setLoadFailed] = useState(false);

  useImperativeHandle(ref, () => ({
    open: async (r: RoleItem) => {
      setRole(r);
      setOpen(true);
      setLoading(true);
      setUsers([]);
      setSelected([]);
      setLoadFailed(false);
      try {
        // 权限分支：平台管理员跨租户全量；其余本租户（角色是租户实体，分配用户仅限本租户）。
        // 本租户走循环分页取全量——只取第一页时，排在后面的合法用户匹配不到 option，
        // antd 会把数字 value 直接当标签显示。
        const cachedUser = readCachedUser<ShellUser>();
        const [all, assigned] = await Promise.all([
          isPlatformAdmin(cachedUser) ? listAllUsers() : listAllTenantUsers(),
          listRoleUsers(r.id),
        ]);
        setUsers(all.items);
        setSelected(assigned);
      } catch (err) {
        setLoadFailed(true);
        message.error(err instanceof Error ? err.message : t('rolePermission.loadUsersFailed'));
      } finally {
        setLoading(false);
      }
    },
  }));

  const handleSave = async () => {
    if (!role) return;
    if (loadFailed) {
      message.error(t('rolePermission.loadUsersFailed'));
      return;
    }
    setSaving(true);
    try {
      await setRoleUsers(role.id, selected);
      message.success(t('rolePermission.usersUpdated'));
      setOpen(false);
      onSaved();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('rolePermission.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setOpen(false);
  };

  // 已绑定但不在用户列表里的 id（硬删/不可见，或分页未覆盖）显式补占位文案，
  // 不要让 antd 把数字 value 直接当标签显示成裸 id。
  const knownUserIds = new Set(users.map((u) => u.id));
  const userOptions = users.map((u) => ({
    value: u.id,
    label: u.display_name || u.username,
  }));
  const orphanOptions = selected
    .filter((id) => !knownUserIds.has(id))
    .map((id) => ({
      value: id,
      label: t('rolePermission.unknownUser', { id }),
    }));

  return (
    <Modal
      title={t('rolePermission.assignUsersTitle', { name: role?.name })}
      open={open}
      onCancel={handleCancel}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      okButtonProps={{ disabled: loadFailed }}
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
          options={[...userOptions, ...orphanOptions]}
        />
      )}
    </Modal>
  );
});

RoleUserAssignModal.displayName = 'RoleUserAssignModal';
export default RoleUserAssignModal;
