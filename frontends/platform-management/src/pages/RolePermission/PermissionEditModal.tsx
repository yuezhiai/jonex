import React, { useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Checkbox, Spin, message } from 'antd';
import { getRolePermissions, setRolePermissions, type RoleItem, type PermissionItem } from '../../api/roles';

export interface PermissionEditModalRef {
  open: (role: RoleItem) => void;
}

interface Props {
  perms: PermissionItem[];
  onSaved: () => void;
}

function permissionName(permission: PermissionItem, t: (key: string) => string) {
  const key = permission.code.replace(':', '.');
  const translated = t(`rolePermission.builtInPermissions.${key}`);
  return translated === `rolePermission.builtInPermissions.${key}` ? permission.name : translated;
}

const PermissionEditModal = forwardRef<PermissionEditModalRef, Props>(({ perms, onSaved }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<RoleItem | null>(null);
  const [checked, setChecked] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);

  useImperativeHandle(ref, () => ({
    open: async (r: RoleItem) => {
      setRole(r);
      setOpen(true);
      setLoading(true);
      try {
        const ids = await getRolePermissions(r.id);
        setChecked(ids);
      } catch (err: any) {
        message.error(err?.message || t('rolePermission.loadPermsFailed'));
      } finally {
        setLoading(false);
      }
    },
  }));

  const handleSave = async () => {
    if (!role) return;
    try {
      await setRolePermissions(role.id, checked);
      message.success(t('rolePermission.permsUpdated'));
      setOpen(false);
      onSaved();
    } catch (err: any) {
      message.error(err?.message || t('rolePermission.saveFailed'));
    }
  };

  const handleCancel = () => {
    setOpen(false);
  };

  return (
    <Modal
      title={t('rolePermission.editPermsTitle', { name: role?.name })}
      open={open}
      onCancel={handleCancel}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      width={600}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}>
            <Checkbox
              indeterminate={checked.length > 0 && checked.length < perms.length}
              checked={checked.length === perms.length}
              onChange={(e) => {
                setChecked(e.target.checked ? perms.map((p) => p.id) : []);
              }}
            >
              <strong>{t('common.selectAll')}</strong>
            </Checkbox>
          </div>
          <Checkbox.Group
            value={checked}
            onChange={(v) => setChecked(v as number[])}
            style={{ width: '100%', display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}
          >
            {perms.map((p) => (
              <Checkbox key={p.id} value={p.id}>
                <strong>{permissionName(p, t)}</strong>
                <br />
                <span style={{ color: '#94a3b8', fontSize: 11 }}>
                  {p.resource}:{p.action}
                </span>
              </Checkbox>
            ))}
          </Checkbox.Group>
        </>
      )}
    </Modal>
  );
});

PermissionEditModal.displayName = 'PermissionEditModal';
export default PermissionEditModal;
