import type { TenantItem } from '../api/tenants';

type Translate = (key: string) => string;

const BUILT_IN_TENANTS: Record<string, { name: string; description: string; key: string }> = {
  tenant_jonex_demo: {
    name: 'Jonex演示租户',
    description: '本地开发与演示租户',
    key: 'demo',
  },
  tenant_jonex_alpha: {
    name: 'Jonex Alpha 测试租户',
    description: '用于多租户登录选择流程测试',
    key: 'alpha',
  },
  tenant_jonex_beta: {
    name: 'Jonex Beta 测试租户',
    description: '用于多租户登录选择流程测试',
    key: 'beta',
  },
};

export function tenantDisplay(tenant: Pick<TenantItem, 'id' | 'name' | 'description'>, t: Translate) {
  const builtIn = BUILT_IN_TENANTS[tenant.id];
  return builtIn && tenant.name === builtIn.name && tenant.description === builtIn.description
    ? {
        name: t(`tenantManagement.builtInTenants.${builtIn.key}.name`),
        description: t(`tenantManagement.builtInTenants.${builtIn.key}.description`),
      }
    : { name: tenant.name, description: tenant.description || '' };
}
