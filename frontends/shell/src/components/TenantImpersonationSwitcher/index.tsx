import React, { useEffect, useState } from 'react';
import { Button, Dropdown } from 'antd';
import { SwapOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { ShellUser } from '@jonex/shell-sdk';
import { listTenants, switchTenant } from '../../api/auth';
import type { TenantListItem } from '../../api/auth';

interface Props {
  user: ShellUser | null;
}

/** 顶栏「切换租户」入口：仅平台管理员、非模拟态可见；模拟态由横幅承接 */
export default function TenantImpersonationSwitcher({ user }: Props) {
  const { t } = useTranslation();
  const [tenants, setTenants] = useState<TenantListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!user?.isPlatformAdmin || user.impersonated) return;
    let cancelled = false;
    setLoading(true);
    listTenants()
      .then((data) => {
        if (!cancelled) setTenants(data.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setTenants([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [user?.isPlatformAdmin, user?.impersonated]);

  if (!user?.isPlatformAdmin || user.impersonated) return null;

  const switchable = tenants.filter((item) => item.id !== user.tenantId);

  return (
    <Dropdown
      placement="bottomRight"
      disabled={loading || switchable.length === 0}
      menu={{
        items: switchable.map((item) => ({
          key: item.id,
          label: item.name,
          onClick: async () => {
            setBusy(true);
            try {
              await switchTenant(item.id);
            } catch {
              // 失败已由响应拦截器 toast
            } finally {
              setBusy(false);
            }
          },
        })),
      }}
    >
      <Button type="text" icon={<SwapOutlined />} loading={busy || loading} style={{ fontSize: 14 }}>
        {t('impersonation.switch')}
      </Button>
    </Dropdown>
  );
}
