import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Tag, message, Spin, Result } from 'antd';
import { EditOutlined, TeamOutlined, PlusOutlined } from '@ant-design/icons';
import { listRoles, listPermissions, type RoleItem, type PermissionItem } from '../../api/roles';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import PermissionEditModal, { type PermissionEditModalRef } from './PermissionEditModal';
import RoleUserAssignModal, { type RoleUserAssignModalRef } from './RoleUserAssignModal';
import NewRoleModal, { type NewRoleModalRef } from './NewRoleModal';

const BUILT_IN_ROLE_KEYS: Record<string, string> = {
  admin: 'systemAdmin',
  user: 'user',
  平台管理员: 'platformAdmin',
  租户管理员: 'systemAdmin',
  领域服务管理员: 'domainServiceAdmin',
  知识编辑者: 'knowledgeEditor',
  观察者: 'observer',
};

function roleCopy(role: RoleItem, t: (key: string) => string) {
  const key = BUILT_IN_ROLE_KEYS[role.name];
  return key
    ? {
        name: t(`rolePermission.builtInRoles.${key}.name`),
        description: t(`rolePermission.builtInRoles.${key}.description`),
      }
    : { name: role.name, description: role.description || '--' };
}

export default function RolePermission() {
  const { t } = useTranslation();
  const cachedUser = readCachedUser<ShellUser>();
  const isPlatformAdminFlag = isPlatformAdmin(cachedUser);
  const [roles, setRoles] = useState<RoleItem[]>([]);
  const [perms, setPerms] = useState<PermissionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const permModalRef = useRef<PermissionEditModalRef>(null);
  const userAssignModalRef = useRef<RoleUserAssignModalRef>(null);
  const newRoleModalRef = useRef<NewRoleModalRef>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [r, p] = await Promise.all([listRoles(), listPermissions()]);
      setRoles(r.items);
      setPerms(p.items);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('rolePermission.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <Result
        status="error"
        title={t('rolePermission.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('rolePermission.pageTitle')}</h1>
        <p style={{ color: '#64748b', margin: '4px 0 0', fontSize: 14 }}>{t('rolePermission.pageSubtitle')}</p>
      </div>
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => newRoleModalRef.current?.open()}>
          {t('rolePermission.newRole')}
        </Button>
      </div>
      <Spin spinning={loading}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 20 }}>
          {roles
            .filter((r) => isPlatformAdminFlag || r.is_system !== 1) // 非平台管理员不可见系统角色（平台管理员）
            .map((r) => {
            const isAdmin = r.is_system === 1;
            const display = roleCopy(r, t);
            return (
              <div
                key={r.id}
                style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 24 }}
              >
                <div
                  style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}
                >
                  <span style={{ fontSize: 16, fontWeight: 600 }}>
                    {display.name}
                    {isAdmin ? t('rolePermission.system') : ''}
                  </span>
                  <span style={{ fontSize: 13, color: '#64748b' }}>
                    <TeamOutlined /> {display.description}
                  </span>
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16, minHeight: 32 }}>
                  {isAdmin ? (
                    <Tag
                      style={{
                        background: '#eff6ff',
                        color: '#3b82f6',
                        border: 'none',
                        borderRadius: 6,
                        padding: '4px 12px',
                      }}
                    >
                      {t('rolePermission.allPermissions')}
                    </Tag>
                  ) : (
                    <Tag style={{ background: '#f1f5f9', color: '#475569', border: 'none' }}>
                      {t('rolePermission.clickToEditPerms')}
                    </Tag>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <Button size="small" icon={<TeamOutlined />} onClick={() => userAssignModalRef.current?.open(r)}>
                    {t('rolePermission.assignUsers')}
                  </Button>
                  <Button
                    type="primary"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => permModalRef.current?.open(r)}
                  >
                    {t('rolePermission.editPerms')}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </Spin>

      <PermissionEditModal ref={permModalRef} perms={perms} onSaved={load} />
      <RoleUserAssignModal ref={userAssignModalRef} onSaved={load} />
      <NewRoleModal ref={newRoleModalRef} onCreated={load} />
    </div>
  );
}
