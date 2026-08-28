import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Input, Button, Table, Tag, Select, Result } from 'antd';
import { SearchOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import { listAllUsers, listUsers, type UserItem } from '../../api/users';
import { listTenants, getTenantUserCounts, type TenantItem } from '../../api/tenants';
import { tenantDisplay } from '../../utils/tenantDisplay';
import UserFormModal, { type UserFormModalHandle } from './UserFormModal';
import ToggleStatusModal, { type ToggleStatusModalHandle } from './ToggleStatusModal';
import DeleteConfirmModal, { type DeleteConfirmModalHandle } from './DeleteConfirmModal';
import './index.css';

export default function UserManagement() {
  const { t } = useTranslation();
  const user = readCachedUser<ShellUser>();
  const isAdmin = isPlatformAdmin(user);
  // 本租户管理能力：平台管理员（跨租户全量）或租户管理员（user:write 码）
  const canManage = isAdmin || user?.isTenantAdmin === true;
  const [users, setUsers] = useState<UserItem[]>([]);
  const [tenants, setTenants] = useState<(TenantItem & { userCount: number })[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState<string>('');
  const [activeTenant, setActiveTenant] = useState(isAdmin ? 'all' : (user?.tenantId ?? 'all'));

  const formModalRef = useRef<UserFormModalHandle>(null);
  const toggleStatusModalRef = useRef<ToggleStatusModalHandle>(null);
  const deleteConfirmModalRef = useRef<DeleteConfirmModalHandle>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 权限分支：平台管理员（platform:user:all）跨租户全量；其余用户本租户（/users 走 user:read）
      const [ur, tr, counts] = await Promise.all([
        isAdmin ? listAllUsers() : listUsers(1, 100),
        listTenants(1, 100),
        getTenantUserCounts(),
      ]);
      setUsers(ur.items);
      setTenants(tr.items.map((t) => ({ ...t, userCount: counts[t.id] || 0 })));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, isAdmin]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = users.filter((u) => {
    if (activeTenant !== 'all' && u.tenant_id !== activeTenant) return false;
    if (roleFilter && !(u.role_names ?? []).includes(roleFilter)) return false;
    if (search) {
      const q = search.toLowerCase();
      if (!u.username.includes(q) && !(u.display_name || '').includes(q) && !(u.email || '').includes(q)) return false;
    }
    return true;
  });

  const openCreate = () => formModalRef.current?.open();
  const openEdit = (u: UserItem) => formModalRef.current?.open(u);

  const displayName = (user: UserItem) => {
    const builtIns: Record<string, { raw: string; key: string }> = {
      'tenant_jonex_demo|admin': {
        raw: '平台管理员',
        key: 'userManagement.builtInUsers.systemAdmin',
      },
      'tenant_jonex_demo|multi_same_pass': {
        raw: '同名同密用户 - 演示租户',
        key: 'userManagement.builtInUsers.multiSameDemo',
      },
      'tenant_jonex_alpha|multi_same_pass': {
        raw: '同名同密用户 - Alpha 租户',
        key: 'userManagement.builtInUsers.multiSameAlpha',
      },
      'tenant_jonex_demo|multi_one_match': {
        raw: '单租户密码匹配用户 - 演示租户',
        key: 'userManagement.builtInUsers.oneMatchDemo',
      },
      'tenant_jonex_alpha|multi_one_match': {
        raw: '单租户密码匹配用户 - Alpha 租户',
        key: 'userManagement.builtInUsers.oneMatchAlpha',
      },
      'tenant_jonex_beta|tenant_header_user': {
        raw: '指定租户登录测试用户 - Beta 租户',
        key: 'userManagement.builtInUsers.tenantHeaderBeta',
      },
    };
    const builtIn = builtIns[`${user.tenant_id}|${user.username}`];
    return builtIn && user.display_name === builtIn.raw ? t(builtIn.key) : user.display_name;
  };

  const roleLabel = (v: string) => {
    if (v === 'admin') return t('auth.systemAdmin');
    if (v === 'user') return t('userManagement.roleUser');
    return v;
  };

  const statusLabel = (v: number) => (v === 1 ? t('status.enabled') : t('status.disabled'));
  const statusColor = (v: number) => (v === 1 ? 'success' : 'error');

  const columns = [
    { title: t('userManagement.username'), dataIndex: 'username', key: 'username', width: 120 },
    {
      title: t('userManagement.displayName'),
      dataIndex: 'display_name',
      key: 'display_name',
      width: 160,
      render: (_: string | null, user: UserItem) => displayName(user),
    },
    { title: t('userManagement.email'), dataIndex: 'email', key: 'email' },
    {
      title: t('userManagement.role'),
      dataIndex: 'role',
      key: 'role',
      width: 200,
      render: (_: string, user: UserItem) => {
        // RBAC 绑定角色（与编辑弹窗 get_roles 同源）；无绑定时回退历史列兜底
        const names = user.role_names ?? [];
        if (names.length === 0) return roleLabel(user.role);
        return (
          <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 4 }}>
            {names.map((n) => (
              <Tag key={n} color="blue">
                {n}
              </Tag>
            ))}
          </span>
        );
      },
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 70,
      render: (v: number) => <Tag color={statusColor(v)}>{statusLabel(v)}</Tag>,
    },
    ...(canManage
      ? [
          {
            title: t('common.actions'),
            key: 'actions',
            width: 200,
            render: (_: unknown, r: UserItem) => (
              <span style={{ whiteSpace: 'nowrap' }}>
                <Button type="link" size="small" onClick={() => openEdit(r)}>
                  {t('common.edit')}
                </Button>
                <Button
                  type="link"
                  size="small"
                  style={{ marginLeft: 8 }}
                  onClick={() => toggleStatusModalRef.current?.open(r)}
                >
                  {r.status === 1 ? t('userManagement.disable') : t('userManagement.enable')}
                </Button>
                <Button
                  type="link"
                  size="small"
                  style={{ marginLeft: 8, color: '#dc2626' }}
                  onClick={() => deleteConfirmModalRef.current?.open(r)}
                >
                  {t('common.delete')}
                </Button>
              </span>
            ),
          },
        ]
      : []),
  ];

  if (error)
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
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
        <h1>{t('userManagement.title')}</h1>
      </div>

      <div className="user-layout">
        <div className="tenant-panel">
          <div className="tenant-panel-header">{t('userManagement.tenantList')}</div>
          <div className="tenant-list">
            {isAdmin && (
              <div
                className={`tenant-item${activeTenant === 'all' ? ' active' : ''}`}
                onClick={() => setActiveTenant('all')}
              >
                <span>{t('userManagement.allTenants')}</span>
                <span className="tenant-count">{users.length}</span>
              </div>
            )}
            {tenants.map((tenant) => (
              <div
                key={tenant.id}
                className={`tenant-item${activeTenant === tenant.id ? ' active' : ''}`}
                onClick={() => setActiveTenant(tenant.id)}
              >
                <span>{tenantDisplay(tenant, t).name}</span>
                <span className="tenant-count">{tenant.userCount}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="user-main">
          <div className="yx-card">
            <div className="yx-toolbar">
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', flex: 1 }}>
                <Input
                  prefix={<SearchOutlined />}
                  placeholder={t('userManagement.searchUsers')}
                  style={{ width: 200 }}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  allowClear
                />
                <Select
                  placeholder={t('userManagement.allRoles')}
                  style={{ width: 160 }}
                  value={roleFilter || undefined}
                  onChange={(v) => setRoleFilter(v || '')}
                  allowClear
                  options={Array.from(
                    new Set(users.flatMap((u) => u.role_names ?? [])),
                  ).map((n) => ({ label: n, value: n }))}
                />
              </div>
              {canManage && (
                <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                  {t('userManagement.createUser')}
                </Button>
              )}
            </div>
            <Table
              columns={columns}
              dataSource={filtered}
              loading={loading}
              rowKey="id"
              pagination={{
                total: filtered.length,
                pageSize: 10,
                showTotal: (total) => t('common.totalPage', { total }),
              }}
              size="middle"
            />
          </div>
        </div>
      </div>

      {canManage && (
        <>
          <UserFormModal ref={formModalRef} tenants={tenants} onSaved={load} />
          <ToggleStatusModal ref={toggleStatusModalRef} getUserDisplayName={displayName} onSaved={load} />
          <DeleteConfirmModal ref={deleteConfirmModalRef} getUserDisplayName={displayName} onSaved={load} />
        </>
      )}
    </div>
  );
}
