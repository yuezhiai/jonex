import type { ShellUser } from '@jonex/shell-sdk';

type Translate = (key: string) => string;

const BUILT_IN_ADMIN_NAMES = new Set(['平台管理员', 'Platform Administrator']);

export function userDisplayName(user: ShellUser, t: Translate): string {
  const raw = user.displayName || user.username;
  return user.username === 'admin' && BUILT_IN_ADMIN_NAMES.has(raw) ? t('auth.systemAdmin') : raw;
}

/**
 * 用户角色标签：按实际绑定角色显示（roles 为后端 get_role_names 返回的
 * 角色名列表，中文名可直接展示）；多角色以 " / " 连接；无角色回退"普通用户"。
 */
export function userRoleLabel(user: ShellUser, t: Translate): string {
  return user.roles?.length ? user.roles.join(' / ') : t('auth.user');
}

export function tenantDisplayName(tenant: { tenant_id: string; tenant_name: string }, t: Translate): string {
  const builtIns: Record<string, { raw: string; key: string }> = {
    tenant_jonex_demo: { raw: '悦溪演示租户', key: 'demo' },
    tenant_jonex_alpha: { raw: '悦溪 Alpha 测试租户', key: 'alpha' },
    tenant_jonex_beta: { raw: '悦溪 Beta 测试租户', key: 'beta' },
  };
  const builtIn = builtIns[tenant.tenant_id];
  return builtIn && tenant.tenant_name === builtIn.raw
    ? t(`shell.builtInTenants.${builtIn.key}`)
    : tenant.tenant_name || tenant.tenant_id;
}
