import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Menu, Result, Tag } from 'antd';
import type { MenuProps, TableColumnsType } from 'antd';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import {
  CommonTable,
  PageContent,
  PageHeader,
  type CommonTableRef,
  type FilterConfig,
  type TableQueryParams,
} from '@jonex/shared-lib';
import { listAllUsers, listUsers, type UserItem } from '../../api/users';
import { listTenants, getTenantUserCounts, type TenantItem } from '../../api/tenants';
import UserFormModal, { type UserFormModalHandle } from './UserFormModal';
import ToggleStatusModal, { type ToggleStatusModalHandle } from './ToggleStatusModal';
import DeleteConfirmModal, { type DeleteConfirmModalHandle } from './DeleteConfirmModal';
import './index.css';

// [jonex] 本页的搜索、角色筛选、分页全部是纯前端的，只作用于「已加载」的数据。
// 所以一次要尽量把数据拉全，并在拉不全时显式告知，避免用户误以为搜不到就是不存在。
/** 本租户视图单次拉取上限。后端 GET /platform/users 的 page_size 上限是 500。 */
const USER_PAGE_SIZE = 500;
/** 跨租户视图上限：后端 UserService.list_all_users 写死 list_shared(0, 10000)。
 *  该端点返回的 total 是 len(items)，永远等于已加载数，检测不到截断，只能比对上限。 */
const ALL_USERS_BACKEND_CAP = 10000;

/** 筛选区字段名。key 同时是 fetch 入参名（CommonTable 的约定）。
 *  必须用 type 而不是 interface：组件的 F 约束是 Record<string, unknown>，
 *  只有 type 的对象字面量才带隐式索引签名。 */
type UserFilters = {
  keyword?: string;
  role?: string;
};

export default function UserManagement() {
  const { t } = useTranslation();
  const user = readCachedUser<ShellUser>();
  const isAdmin = isPlatformAdmin(user);
  // 本租户管理能力：平台管理员（跨租户全量）或租户管理员。
  // [jonex] 权限重构 B1：isTenantAdmin 的后端判据已由 `user:write` 改为 `tenant:admin`
  // （platform:admin 为上位）。这里读的是后端下发的布尔值，**不要**改成前端自己按
  // 权限码推断 —— 现状 §10.1 的缺陷正是前端按 user:write 自行判断、与后端口径不一致，
  // 导致只配了 user:write 的自定义角色「看得到管理入口但点不动」。
  const canManage = isAdmin || user?.isTenantAdmin === true;
  // [jonex] 服务端总数 > 已加载条数时为真。搜索/筛选/分页都是纯前端的（只作用于已加载
  // 数据），静默截断会让人以为「搜不到就是没有」，故显式提示。
  const [truncated, setTruncated] = useState(false);
  const [tenants, setTenants] = useState<(TenantItem & { userCount: number })[]>([]);
  const [totalUsers, setTotalUsers] = useState(0);
  const [roleOptions, setRoleOptions] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [activeTenant, setActiveTenant] = useState(isAdmin ? 'all' : (user?.tenantId ?? 'all'));

  const tableRef = useRef<CommonTableRef>(null);
  /**
   * 已加载的全量用户。翻页与改筛选都在这份数据上切片 —— 本页的筛选是纯前端的，
   * 每次翻页都重新拉一遍全量（跨租户上限 10000 条）纯属浪费。
   * 需要真正重取时把它置空并让表格 refresh（见 `reload`）。
   */
  const usersRef = useRef<UserItem[] | null>(null);

  const formModalRef = useRef<UserFormModalHandle>(null);
  const toggleStatusModalRef = useRef<ToggleStatusModalHandle>(null);
  const deleteConfirmModalRef = useRef<DeleteConfirmModalHandle>(null);

  /** 拉全量用户并缓存；顺带更新截断提示与角色候选 */
  const loadUsers = useCallback(async () => {
    const res = isAdmin ? await listAllUsers() : await listUsers(1, USER_PAGE_SIZE);
    usersRef.current = res.items;
    setTotalUsers(res.items.length);
    setTruncated(
      isAdmin
        ? res.items.length >= ALL_USERS_BACKEND_CAP
        : typeof res.total === 'number' && res.total > res.items.length,
    );
    setRoleOptions(Array.from(new Set(res.items.flatMap((u) => u.role_names ?? []))));
    return res.items;
  }, [isAdmin]);

  /** 左侧租户列表与各租户用户数 */
  const loadTenantPanel = useCallback(async () => {
    const [res, counts] = await Promise.all([listTenants(1, 100), getTenantUserCounts()]);
    setTenants(res.items.map((item) => ({ ...item, userCount: counts[item.id] || 0 })));
  }, []);

  useEffect(() => {
    loadTenantPanel().catch((e: unknown) => {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    });
  }, [loadTenantPanel, t]);

  /**
   * 丢弃缓存并让表格重取。用于「刷新」与增删改之后。
   *
   * 刷新按钮因此由页面自己摆（`actions.refresh=false`）：CommonTable 自带的那枚只会
   * 重跑 fetch，而 fetch 命中缓存时并不会真的重新请求后端，点了等于没点。
   */
  const reload = useCallback(() => {
    usersRef.current = null;
    tableRef.current?.refresh({ resetPage: true });
  }, []);

  const handleRetry = () => {
    usersRef.current = null;
    setError(null);
    loadTenantPanel().catch(() => {});
  };

  const handleSaved = async () => {
    // 增删改都会影响租户侧的用户计数
    await loadTenantPanel().catch(() => {});
    reload();
  };

  /** 租户切换：数据已在缓存里，只需按新租户重新切片并回到第 1 页 */
  const tenantMounted = useRef(false);
  useEffect(() => {
    if (!tenantMounted.current) {
      tenantMounted.current = true;
      return;
    }
    tableRef.current?.refresh({ resetPage: true });
  }, [activeTenant]);

  const fetchUsers = useCallback(
    async ({ page, pageSize, keyword, role }: TableQueryParams<UserFilters>) => {
      const all = usersRef.current ?? (await loadUsers());
      const q = (keyword ?? '').trim().toLowerCase();
      const filtered = all.filter((item) => {
        if (activeTenant !== 'all' && item.tenant_id !== activeTenant) return false;
        if (role && !(item.role_names ?? []).includes(role)) return false;
        if (q) {
          // [jonex] 两侧都转小写。此前只对关键词做了 toLowerCase，被比较的字段没转，
          // 导致含大写字母的用户名/邮箱搜不到。
          const hay = [item.username, item.display_name || '', item.email || ''].map((s) => s.toLowerCase());
          if (!hay.some((s) => s.includes(q))) return false;
        }
        return true;
      });
      const start = (page - 1) * pageSize;
      return { list: filtered.slice(start, start + pageSize), total: filtered.length };
    },
    [activeTenant, loadUsers],
  );

  const openCreate = () => formModalRef.current?.open();
  const openEdit = (u: UserItem) => formModalRef.current?.open(u);

  const displayName = (row: UserItem) => {
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
    const builtIn = builtIns[`${row.tenant_id}|${row.username}`];
    return builtIn && row.display_name === builtIn.raw ? t(builtIn.key) : row.display_name;
  };

  const roleLabel = (v: string) => {
    if (v === 'admin') return t('auth.systemAdmin');
    if (v === 'user') return t('userManagement.roleUser');
    return v;
  };

  const statusLabel = (v: number) => (v === 1 ? t('status.enabled') : t('status.disabled'));
  const statusColor = (v: number) => (v === 1 ? 'success' : 'error');

  const columns: TableColumnsType<UserItem> = [
    { title: t('userManagement.username'), dataIndex: 'username', key: 'username', width: 120 },
    {
      title: t('userManagement.displayName'),
      dataIndex: 'display_name',
      key: 'display_name',
      width: 160,
      render: (_: string | null, row: UserItem) => displayName(row),
    },
    { title: t('userManagement.email'), dataIndex: 'email', key: 'email' },
    {
      title: t('userManagement.role'),
      dataIndex: 'role',
      key: 'role',
      width: 200,
      render: (_: string, row: UserItem) => {
        // RBAC 绑定角色（与编辑弹窗 get_roles 同源）；无绑定时回退历史列兜底
        const names = row.role_names ?? [];
        if (names.length === 0) return roleLabel(row.role);
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
            render: (_: unknown, row: UserItem) => (
              <span style={{ whiteSpace: 'nowrap' }}>
                <Button type="link" size="small" onClick={() => openEdit(row)}>
                  {t('common.edit')}
                </Button>
                <Button
                  type="link"
                  size="small"
                  style={{ marginLeft: 8 }}
                  onClick={() => toggleStatusModalRef.current?.open(row)}
                >
                  {row.status === 1 ? t('userManagement.disable') : t('userManagement.enable')}
                </Button>
                <Button
                  type="link"
                  size="small"
                  style={{ marginLeft: 8, color: '#dc2626' }}
                  onClick={() => deleteConfirmModalRef.current?.open(row)}
                >
                  {t('common.delete')}
                </Button>
              </span>
            ),
          },
        ]
      : []),
  ];

  const filters: FilterConfig[] = [
    {
      key: 'keyword',
      label: t('userManagement.username'),
      type: 'input',
      placeholder: t('userManagement.searchUsers'),
    },
    {
      key: 'role',
      label: t('userManagement.role'),
      type: 'select',
      placeholder: t('userManagement.allRoles'),
      options: roleOptions.map((name) => ({ label: name, value: name })),
    },
  ];

  /** 左侧租户列表。平台管理员多一个「全部租户」，其余用户只看得到自己所属租户 */
  const tenantMenuItems = useMemo<MenuProps['items']>(() => {
    const tenantItem = (key: string, name: string, count: number) => ({
      key,
      label: (
        <span className="user-tenant-item">
          {/* 名称截断时靠原生 title 兜底，否则长租户名只能看到省略号 */}
          <span className="user-tenant-name" title={name}>
            {name}
          </span>
          <span className="user-tenant-count">{count}</span>
        </span>
      ),
    });
    const items: MenuProps['items'] = tenants.map((tenant) => tenantItem(tenant.id, tenant.name, tenant.userCount));
    if (isAdmin) {
      items.unshift(tenantItem('all', t('userManagement.allTenants'), totalUsers));
      // 「全部租户」是一个汇总项，不是列表成员，用分割线把它和下面的租户分开
      if (tenants.length > 0) items.splice(1, 0, { type: 'divider', key: 'tenant-divider' });
    }
    return items;
  }, [tenants, isAdmin, totalUsers, t]);

  if (error)
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={handleRetry}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  return (
    <>
      <PageHeader title={t('userManagement.title')} />
      <PageContent>
        {truncated && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('userManagement.listTruncated', {
              loaded: totalUsers,
              defaultValue: '用户数超出单次加载上限，当前仅展示前 {{loaded}} 条。搜索与筛选只在已加载数据内生效。',
            })}
          />
        )}

        <div className="user-layout">
          <aside className="user-sidebar">
            <Menu
              className="user-tenant-menu"
              mode="inline"
              // 根级项默认带 24px 缩进；本列表没有子级，缩进白白吃掉租户名的宽度
              inlineIndent={0}
              selectedKeys={[activeTenant]}
              items={tenantMenuItems}
              onClick={({ key }) => setActiveTenant(key)}
            />
          </aside>

          <div className="user-main">
            <CommonTable<UserItem, UserFilters>
              ref={tableRef}
              className="user-table"
              // y 让 antd 把表头/表体拆成两块（表头固定、表体滚动），高度由 CSS 的
              // flex 链给出；不传 y 时表头表体是同一张 table，行多了一定会被裁
              scroll={{ x: 'max-content', y: '100%' }}
              filters={filters}
              filterColsPerRow={3}
              columns={columns}
              rowKey="id"
              fetch={fetchUsers}
              onError={(e) => setError(e instanceof Error ? e.message : t('common.loadFailed'))}
              // 内置刷新只会重跑 fetch，而 fetch 命中本地缓存时不会真的重新请求；
              // 刷新改由 toolbar 里的按钮清缓存后重取
              actions={{ refresh: false }}
              toolbar={
                <>
                  <Button icon={<ReloadOutlined />} onClick={reload}>
                    {t('common.refresh')}
                  </Button>
                  {canManage && (
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                      {t('userManagement.createUser')}
                    </Button>
                  )}
                </>
              }
              size="middle"
              defaultPageSize={10}
            />
          </div>
        </div>

        {canManage && (
          <>
            <UserFormModal ref={formModalRef} tenants={tenants} onSaved={handleSaved} />
            <ToggleStatusModal ref={toggleStatusModalRef} getUserDisplayName={displayName} onSaved={handleSaved} />
            <DeleteConfirmModal ref={deleteConfirmModalRef} getUserDisplayName={displayName} onSaved={handleSaved} />
          </>
        )}
      </PageContent>
    </>
  );
}
