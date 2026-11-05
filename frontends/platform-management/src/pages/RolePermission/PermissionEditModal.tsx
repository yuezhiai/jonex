import React, { useCallback, useEffect, useRef, useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Checkbox, Col, Row, Spin, Typography, message } from 'antd';
import { getRolePermissions, setRolePermissions, type RoleItem, type PermissionItem } from '../../api/roles';
import './PermissionEditModal.css';

export interface PermissionEditModalRef {
  open: (role: RoleItem) => void;
}

interface Props {
  perms: PermissionItem[];
  onSaved: () => void;
}

function permissionName(permission: PermissionItem, t: (key: string) => string) {
  const key = permission.code.replace(/:/g, '.');
  const translated = t(`rolePermission.builtInPermissions.${key}`);
  return translated === `rolePermission.builtInPermissions.${key}` ? permission.name : translated;
}

type PermissionGroupKey = 'platform' | 'tenant' | 'business';

const PERMISSION_GROUP_ORDER: PermissionGroupKey[] = ['platform', 'tenant', 'business'];

const PLATFORM_CODES = new Set([
  'datasource:read', 'datasource:write',
  'engine:read', 'engine:write',
  'adapter:read', 'adapter:write',
  'model:read', 'model:write',
  // 数据运营：码本身是租户级（见 deploy/postgres/migrations/006_seed_data.sql），
  // 只按产品要求归到「平台管理」组展示。
  'analytics:read',
]);

const TENANT_ADMIN_CODES = new Set([
  'tenant:write',
  'user:read', 'user:write',
  'role:read', 'role:write',
  'tenant:admin',
  'space:write',
  'audit:read',
]);

function permissionGroupKey(permission: PermissionItem): PermissionGroupKey {
  if (permission.code.startsWith('platform:') || PLATFORM_CODES.has(permission.code)) {
    return 'platform';
  }
  if (TENANT_ADMIN_CODES.has(permission.code)) {
    return 'tenant';
  }
  // 兜底落「业务功能」：`task_center:*` 的码本身是租户级（不带 `platform:` 前缀），
  // 归到这里是按产品要求展示，不是在按码的层级归类。
  return 'business';
}

const PermissionEditModal = forwardRef<PermissionEditModalRef, Props>(({ perms, onSaved }, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<RoleItem | null>(null);
  const [checked, setChecked] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  // macOS 的滚动条不滚就隐身，底部被视口边缘切一半的那行看着像内容丢了，用渐隐提示还能往下滚
  const bodyRef = useRef<HTMLDivElement>(null);
  const [hasMore, setHasMore] = useState(false);

  const syncHasMore = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    setHasMore(el.scrollTop + el.clientHeight < el.scrollHeight - 2);
  }, []);

  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(syncHasMore);
    return () => cancelAnimationFrame(id);
  }, [open, loading, syncHasMore]);

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

  const grouped = PERMISSION_GROUP_ORDER.map((key) => ({
    key,
    items: perms.filter((p) => permissionGroupKey(p) === key),
  })).filter((group) => group.items.length > 0);

  const checkedSet = new Set(checked);
  const toggle = (id: number, on: boolean) =>
    setChecked((prev) => (on ? [...prev, id] : prev.filter((x) => x !== id)));
  const toggleMany = (ids: number[], on: boolean) =>
    setChecked((prev) => (on ? Array.from(new Set([...prev, ...ids])) : prev.filter((x) => !ids.includes(x))));

  return (
    <Modal
      title={t('rolePermission.editPermsTitle', { name: role?.name })}
      open={open}
      onCancel={handleCancel}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      width={760}
      centered
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      ) : (
        // 权限项在几十条量级，弹窗按内容长高会顶穿视口、把底部按钮挤出屏幕，
        // 所以滚动限定在正文容器上（高度见 .perm-body）
        <div className="perm-body" ref={bodyRef} onScroll={syncHasMore}>
          <div className="perm-toolbar">
            <Checkbox
              indeterminate={checked.length > 0 && checked.length < perms.length}
              checked={checked.length === perms.length && perms.length > 0}
              onChange={(e) => {
                setChecked(e.target.checked ? perms.map((p) => p.id) : []);
              }}
            >
              <strong>{t('common.selectAll')}</strong>
            </Checkbox>
            <Typography.Text type="secondary">
              {checked.length} / {perms.length}
            </Typography.Text>
          </div>

          {grouped.map((group) => {
            const ids = group.items.map((p) => p.id);
            const selected = ids.filter((id) => checkedSet.has(id)).length;
            return (
              <div className="perm-group" key={group.key}>
                <div className="perm-group-head">
                  <Typography.Text strong>{t(`rolePermission.permissionGroups.${group.key}`)}</Typography.Text>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}>
                    <Typography.Text type="secondary">
                      {selected} / {ids.length}
                    </Typography.Text>
                    <Checkbox
                      checked={selected === ids.length}
                      indeterminate={selected > 0 && selected < ids.length}
                      onChange={(e) => toggleMany(ids, e.target.checked)}
                    >
                      <Typography.Text type="secondary">{t('common.selectAll')}</Typography.Text>
                    </Checkbox>
                  </span>
                </div>
                <Row gutter={[8, 8]}>
                  {group.items.map((p) => (
                    <Col key={p.id} xs={24} sm={12} lg={8}>
                      <div className="perm-item">
                        <Checkbox checked={checkedSet.has(p.id)} onChange={(e) => toggle(p.id, e.target.checked)}>
                          <div className="perm-item-name">{permissionName(p, t)}</div>
                          <div className="perm-item-code">{p.code}</div>
                        </Checkbox>
                      </div>
                    </Col>
                  ))}
                </Row>
              </div>
            );
          })}
          {hasMore ? <div className="perm-body-fade" aria-hidden /> : null}
        </div>
      )}
    </Modal>
  );
});

PermissionEditModal.displayName = 'PermissionEditModal';
export default PermissionEditModal;
