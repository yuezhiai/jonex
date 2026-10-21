import React, { useMemo } from 'react';
import { LogoutOutlined } from '@ant-design/icons';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { switchLocale } from '@jonex/i18n-resources';
import { useStore } from '@/store';
import { usePermission } from '@jonex/shared-lib';
import { getMenuFromRoutes, hasMenuPermission, IconMap } from '@/router/menu';
import type { MenuItem } from '@/router/menu';
import { getRoutes } from '@/router/routes.config';
import { clearAuthStorage, finishLogout, revokeServerSession } from '@jonex/shell-sdk';
import { AppLayout } from '@jonex/platform-theme';
import type { LayoutNavItem } from '@jonex/platform-theme';

const BasicLayout = () => {
  const { global } = useStore();
  const userInfo = global?.userInfo as Record<string, any> | null | undefined;
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  const VITE_LOGIN = (import.meta as any).env?.VITE_LOGIN || '/login';
  const VITE_APP_ID = (import.meta as any).env?.VITE_APP_ID || 'platform-management';

  // 权限（shell 下发）：由共享 hook usePermission 统一提供
  const { permissions: userPermissions } = usePermission();

  // 方案二：菜单从路由配置生成（routes.config.ts 的 menu 元数据）
  const allMenuItems = useMemo(() => getMenuFromRoutes(getRoutes(), t), [t]);

  const visibleMenuItems = useMemo(() => {
    // 判定收敛到 hasMenuPermission：支持 permissionCode 为码数组（任一命中即可），
    // 与 router/index.tsx 的守卫同一套语义，避免出现「菜单看得见但点进去 403」的两头不一致。
    return allMenuItems.filter((item) => hasMenuPermission(item.permissionCode, userPermissions));
  }, [allMenuItems, userPermissions]);

  // 路由菜单元 → 引擎受控数据（icon 名解析为 ReactNode）
  const toNavItem = (item: MenuItem): LayoutNavItem => {
    const IconComp = item.icon ? IconMap[item.icon] : undefined;
    return {
      key: item.key,
      label: item.label,
      path: item.path,
      icon: IconComp ? <IconComp /> : undefined,
    };
  };

  // 与既有视觉一致：单 section（site.title 标题）+ 扁平菜单项；无菜单时不出导航
  const navItems: LayoutNavItem[] = visibleMenuItems.length
    ? [{ key: 'root', label: t('site.title'), children: visibleMenuItems.map(toNavItem) }]
    : [];

  const handleLogout = async () => {
    // SSO 会话返回 sso_logout_url 时由 finishLogout 走顶层导航到 Keycloak 结束会话
    const ssoLogoutUrl = await revokeServerSession();
    clearAuthStorage({ keepLocale: true });
    global.setUserInfo(null);
    finishLogout(ssoLogoutUrl, { loginUrl: VITE_LOGIN || '/login', appId: VITE_APP_ID });
  };

  return (
    <AppLayout
      logo={{ href: '/model-adapter', alt: t('site.title'), locale: i18n.language }}
      navItems={navItems}
      activePath={location.pathname}
      onNavigate={(path) => navigate(path)}
      topbarRight={{
        language: {
          current: i18n.language,
          onSwitch: (key: string) => switchLocale(i18n, key),
        },
        user: userInfo
          ? {
              avatarText: userInfo.realName || userInfo.username || 'U',
              actions: [{ key: 'logout', icon: <LogoutOutlined />, label: t('auth.signOut'), onClick: handleLogout }],
            }
          : undefined,
      }}
    >
      <Outlet />
    </AppLayout>
  );
};

export default BasicLayout;
