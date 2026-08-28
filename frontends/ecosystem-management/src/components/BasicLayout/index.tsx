import React, { useState, useEffect, useMemo } from 'react';
import { Layout, Dropdown, Button } from 'antd';
import { LogoutOutlined, HomeOutlined, GlobalOutlined, CaretDownOutlined } from '@ant-design/icons';
import { Outlet, useNavigate, useLocation, Link, useMatches, matchRoutes } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { LANGUAGE_OPTIONS, LANGUAGE_STORAGE_KEY } from '@jonex/i18n-resources';
import { useStore } from '@/store';
import { usePermission } from '@jonex/shared-lib';
import { getMenuFromRoutes, IconMap } from '@/router/menu';
import type { MenuItem } from '@/router/menu';
import { getRoutes } from '@/router/routes.config';
import { buildLoginRedirectUrl, clearAuthStorage } from '@jonex/shell-sdk';
import styles from './index.module.scss';

const { Content } = Layout;

const BasicLayout = () => {
  const { global } = useStore();
  const userInfo = global?.userInfo as Record<string, any> | null | undefined;
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const matches = useMatches();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const VITE_LOGIN = (import.meta as any).env?.VITE_LOGIN || '/login';
  const VITE_APP_ID = (import.meta as any).env?.VITE_APP_ID || 'ecosystem-management';

  // 权限（shell 下发）：由共享 hook usePermission 统一提供
  const { permissions: userPermissions } = usePermission();

  // 方案二：菜单从路由配置生成（routes.config.ts 的 menu 元数据）
  const allMenuItems = useMemo(() => getMenuFromRoutes(getRoutes(), t), [t]);

  const visibleMenuItems = useMemo(() => {
    return allMenuItems.filter((item) => {
      if (!item.permissionCode) return true;
      return userPermissions.includes(item.permissionCode);
    });
  }, [allMenuItems, userPermissions]);

  const currentTitle = useMemo(() => {
    const routeMatches = matchRoutes(getRoutes() as any[], location);
    const matched = routeMatches?.reverse().find((m) => (m.route as Record<string, unknown>)?.title);
    return ((matched?.route as Record<string, unknown>)?.title as string) || '';
  }, [location]);

  useEffect(() => {
    const isMobile = window.innerWidth < 768;
    if (isMobile) setSidebarCollapsed(true);
  }, []);

  const isItemActive = (item: MenuItem): boolean => {
    if (!item.path) return false;
    return location.pathname === item.path || location.pathname.startsWith(item.path + '/');
  };

  const handleLogout = () => {
    clearAuthStorage({ keepLocale: true });
    global.setUserInfo(null);
    const loginUrl = VITE_LOGIN || '/login';
    window.location.href = buildLoginRedirectUrl(loginUrl, window.location.href, VITE_APP_ID);
  };

  const renderNavIcon = (iconName?: string) => {
    if (!iconName) return <span className="yx-sub-dot" />;
    const IconComp = IconMap[iconName];
    if (!IconComp) return <span className="yx-sub-dot" />;
    return (
      <span className="yx-nav-icon">
        <IconComp />
      </span>
    );
  };

  const sidebarWidth = sidebarCollapsed ? 64 : 240;

  return (
    <div className={styles['page-layout']}>
      <aside className="yx-sidebar" style={{ width: sidebarWidth }}>
        <div className={styles['sidebar-brand']}>
          <Link
            to="/adapter-management"
            style={{
              textDecoration: 'none',
              color: 'inherit',
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <img
              src={sidebarCollapsed ? '/favicon.png' : i18n.language === 'en' ? '/logo-en.svg' : '/logo.svg'}
              alt="Jonex"
              style={{ height: sidebarCollapsed ? 36 : 32, transition: 'height 0.2s' }}
            />
          </Link>
        </div>

        <nav className={styles['sidebar-nav']}>
          {!sidebarCollapsed && <div className="yx-nav-section">{t('site.title')}</div>}
          {visibleMenuItems.map((item) => (
            <a
              key={item.key}
              className={`yx-nav-item${isItemActive(item) ? ' active' : ''}`}
              onClick={(e) => {
                e.preventDefault();
                navigate(item.path!);
              }}
              style={{ cursor: 'pointer', textDecoration: 'none' }}
            >
              {renderNavIcon(item.icon)}
              {!sidebarCollapsed && <span>{t(item.label)}</span>}
            </a>
          ))}
        </nav>
      </aside>

      <div className={styles['main-area']}>
        <header className="yx-topbar">
          <div className="yx-breadcrumb">
            <HomeOutlined style={{ marginRight: 6 }} />
            <span className="current">{currentTitle ? t(currentTitle) : t('site.title')}</span>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Dropdown
              menu={{
                items: LANGUAGE_OPTIONS.filter((o) => o.value !== i18n.language).map((o) => ({
                  key: o.value,
                  label: t(`language.${o.value}`, { defaultValue: o.label }),
                })),
                onClick: ({ key }) => {
                  i18n.changeLanguage(key);
                  localStorage.setItem(LANGUAGE_STORAGE_KEY, key);
                  window.dispatchEvent(new CustomEvent('jonex:locale-change', { detail: key }));
                },
              }}
              placement="bottomRight"
              trigger={['click']}
            >
              <Button
                type="text"
                style={{
                  height: 38,
                  borderRadius: 10,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  fontSize: 14,
                  color: 'inherit',
                  padding: '0 10px',
                }}
              >
                <GlobalOutlined style={{ fontSize: 16 }} />
                <span>
                  {(() => {
                    const option = LANGUAGE_OPTIONS.find((o) => o.value === i18n.language);
                    return option ? t(`language.${option.value}`, { defaultValue: option.label }) : i18n.language;
                  })()}
                </span>
                <CaretDownOutlined style={{ fontSize: 10, color: '#94a3b8' }} />
              </Button>
            </Dropdown>
            {userInfo && (
              <Dropdown
                menu={{
                  items: [
                    {
                      key: 'logout',
                      icon: <LogoutOutlined />,
                      label: t('auth.signOut'),
                      onClick: handleLogout,
                    },
                  ],
                }}
                placement="bottomRight"
              >
                <div className={styles['user-avatar']}>
                  {(userInfo.realName || userInfo.username || 'U').charAt(0).toUpperCase()}
                </div>
              </Dropdown>
            )}
          </div>
        </header>

        <main className="yx-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
};

export default BasicLayout;
