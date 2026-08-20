import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { Button, Layout, Dropdown, Spin } from 'antd';
import { useTranslation } from 'react-i18next';
import { LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, HomeOutlined, RightOutlined } from '@ant-design/icons';
import * as Icons from '@ant-design/icons';
import { useNavigate, useLocation, Link } from 'react-router-dom';
import { getUser, logout } from '../../api/auth';
import { fetchAppManifest, getEnabledApps } from '../../api/manifest';
import { fetchMyMenus } from '../../api/menus';
import type { MenuNode } from '../../api/menus';
import SpaceSwitcher from '../SpaceSwitcher';
import LocaleSwitcher from '../LocaleSwitcher';
import type { AppManifestEntry } from '@jonex/shell-sdk';
import { colors } from '@jonex/platform-theme/tokens';
import { userDisplayName } from '../../utils/userDisplay';

interface BreadcrumbItem {
  title: string;
  path: string;
}

/** 当前路由是否命中菜单项 path（含子路由） */
const isPathActive = (path: string | null, pathname: string): boolean =>
  !!path && (pathname === path || pathname.startsWith(path + '/'));

/** 子树内是否存在命中 pathname 的叶子节点 */
const subtreeActive = (node: MenuNode, pathname: string): boolean => {
  if (node.children?.length) return node.children.some((child) => subtreeActive(child, pathname));
  return isPathActive(node.path, pathname);
};

/** 收集 pathname 所在子树经过的全部分组 key（用于默认展开） */
const collectActiveGroupKeys = (nodes: MenuNode[], pathname: string): string[] => {
  const keys: string[] = [];
  for (const node of nodes) {
    if (node.children?.length) {
      if (subtreeActive(node, pathname)) keys.push(String(node.id));
      keys.push(...collectActiveGroupKeys(node.children, pathname));
    }
  }
  return keys;
};

/** 在菜单树中查找匹配 pathname 的叶子节点及其祖先链 */
const findMenuLeafChain = (
  nodes: MenuNode[],
  pathname: string,
  ancestors: MenuNode[] = [],
): { chain: MenuNode[]; leaf: MenuNode } | null => {
  for (const node of nodes) {
    if (node.children?.length) {
      const found = findMenuLeafChain(node.children, pathname, [...ancestors, node]);
      if (found) return found;
    } else if (isPathActive(node.path, pathname)) {
      return { chain: ancestors, leaf: node };
    }
  }
  return null;
};

/** antd 图标名（如 'SearchOutlined'）→ 图标组件实例 */
const renderMenuIcon = (iconName: string | null): React.ReactNode => {
  if (!iconName) return null;
  const IconCmp = (Icons as unknown as Record<string, React.ComponentType | undefined>)[iconName];
  return IconCmp ? <IconCmp /> : null;
};

export default function AppShellLayout({ children }: { children: React.ReactNode }) {
  const { t, i18n } = useTranslation();
  const [user] = useState(() => getUser());
  const navigate = useNavigate();
  const location = useLocation();
  const [apps, setApps] = useState<AppManifestEntry[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [menus, setMenus] = useState<MenuNode[]>([]);
  const [menuLoading, setMenuLoading] = useState(true);

  const userRoles = useMemo<string[]>(() => {
    return user?.roles ?? ((user as any)?.role ? [(user as any).role as string] : []);
  }, [user]);

  useEffect(() => {
    fetchAppManifest()
      .then((manifest) => {
        setApps(getEnabledApps(manifest, userRoles));
      })
      .catch(() => {});
  }, [userRoles]);

  // 侧边栏菜单来自后端 /menus/my（按用户权限码过滤后的菜单树）
  useEffect(() => {
    fetchMyMenus()
      .then(setMenus)
      .catch(() => setMenus([]))
      .finally(() => setMenuLoading(false));
  }, []);

  const isMobile = typeof window !== 'undefined' && window.innerWidth < 768;
  useEffect(() => {
    if (isMobile) setSidebarCollapsed(true);
  }, [isMobile]);

  // 默认展开当前路由所在组：menus 首次加载即初始化；后续只合并不收回手动状态
  useEffect(() => {
    if (!menus.length) return;
    const keys = collectActiveGroupKeys(menus, location.pathname);
    if (keys.length) {
      setExpandedKeys((prev) => Array.from(new Set([...prev, ...keys])));
    }
  }, [location.pathname, menus]);

  const toggleGroup = useCallback((key: string) => {
    setExpandedKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  }, []);

  const currentAppId = useMemo(() => {
    const match = location.pathname.match(/^\/apps\/([^/]+)/);
    return match ? match[1] : null;
  }, [location.pathname]);

  const userMenuItems = {
    items: [
      {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: t('auth.logout'),
        onClick: () => {
          logout();
          navigate('/login');
        },
      },
    ],
  };

  const breadcrumbItems = useMemo<BreadcrumbItem[]>(() => {
    const home: BreadcrumbItem = { title: t('navigation.home'), path: '/' };
    const found = findMenuLeafChain(menus, location.pathname);
    if (!found) return [home];
    const items: BreadcrumbItem[] = [home];
    for (const ancestor of found.chain) {
      items.push({ title: t(ancestor.name), path: '#' });
    }
    items.push({ title: t(found.leaf.name), path: found.leaf.path ?? '#' });
    return items;
  }, [menus, location.pathname, t]);

  const sidebarWidth = sidebarCollapsed ? 64 : 240;

  // 直接导航项（旧版 yx-nav-item 视觉：图标 + 名称）
  const renderDirectItem = (node: MenuNode) => {
    const active = isPathActive(node.path, location.pathname);
    const icon = renderMenuIcon(node.icon);
    return (
      <a
        key={node.id}
        className={`yx-nav-item${active ? ' active' : ''}`}
        onClick={(e) => {
          e.preventDefault();
          if (node.path) navigate(node.path);
        }}
        style={{ cursor: 'pointer', textDecoration: 'none' }}
      >
        {icon ? <span className="yx-nav-icon">{icon}</span> : <span className="yx-sub-dot" />}
        {!sidebarCollapsed && <span>{t(node.name)}</span>}
      </a>
    );
  };

  // 组内子项（旧版 yx-sub-item 视觉：圆点 + 名称）
  const renderSubItem = (node: MenuNode) => {
    const active = isPathActive(node.path, location.pathname);
    return (
      <div
        key={node.id}
        className={`yx-sub-item${active ? ' active' : ''}`}
        onClick={() => {
          if (node.path) navigate(node.path);
        }}
        style={{ cursor: 'pointer' }}
      >
        <span className="yx-sub-dot" />
        <span>{t(node.name)}</span>
      </div>
    );
  };

  // 中间组（旧版 yx-nav-group 视觉：组头可折叠 + 箭头）
  const renderGroupNode = (node: MenuNode) => {
    const key = String(node.id);
    const expanded = expandedKeys.includes(key);
    const children = node.children ?? [];
    const hasActiveChild = children.some((child) => subtreeActive(child, location.pathname));
    const icon = renderMenuIcon(node.icon);

    return (
      <div key={key} className={`yx-nav-group${expanded ? ' open' : ''}`}>
        <div
          className={`yx-nav-group-header${hasActiveChild && !expanded ? ' active' : ''}`}
          onClick={() => toggleGroup(key)}
        >
          {icon ? <span className="yx-nav-icon">{icon}</span> : <span className="yx-sub-dot" />}
          {!sidebarCollapsed && <span>{t(node.name)}</span>}
          {!sidebarCollapsed && <RightOutlined className="yx-nav-arrow" />}
        </div>
        <div className="yx-sub-menu">{children.map(renderSubItem)}</div>
      </div>
    );
  };

  // 顶层分组：section 标签 + 子节点（组或直接项）
  const renderMenuNode = (node: MenuNode): React.ReactNode => {
    const children = node.children ?? [];
    return (
      <div key={node.id} style={{ marginBottom: 4 }}>
        {!sidebarCollapsed && children.length > 0 && (
          <div className="yx-nav-section">{t(node.name)}</div>
        )}
        {children.map((child) =>
          child.children?.length ? renderGroupNode(child) : renderDirectItem(child),
        )}
      </div>
    );
  };

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* Sidebar */}
      <aside className="yx-sidebar" style={{ width: sidebarWidth }}>
        <div
          style={{
            padding: sidebarCollapsed ? '16px 0' : '24px 20px 20px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: sidebarCollapsed ? 'center' : 'flex-start',
            borderBottom: `1px solid ${colors.sidebarBorder}`,
          }}
        >
          <Link
            to="/apps/core-business/knowledge-search"
            style={{
              textDecoration: 'none',
              color: 'inherit',
              display: 'flex',
              alignItems: 'center',
              gap: 12,
            }}
          >
            <img
              src={sidebarCollapsed ? '/favicon.png' : i18n.language === 'en' ? '/logo-en.svg' : '/logo.svg'}
              alt="Jonex"
              style={{
                height: sidebarCollapsed ? 36 : 32,
                transition: 'height 0.2s',
              }}
            />
          </Link>
        </div>

        <SpaceSwitcher collapsed={sidebarCollapsed} />

        <nav style={{ flex: 1, overflowY: 'auto', padding: '12px 0' }}>
          {menuLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '32px 0' }}>
              <Spin size="small" />
            </div>
          ) : (
            menus.map(renderMenuNode)
          )}
        </nav>
      </aside>

      {/* Main Area */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
        }}
      >
        {/* Topbar */}
        <header className="yx-topbar">
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <Button
              type="text"
              icon={sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setSidebarCollapsed((prev) => !prev)}
              style={{ fontSize: 18, width: 36, height: 36 }}
            />
            <div className="yx-breadcrumb">
              {breadcrumbItems.map((item, i) => (
                <span key={i}>
                  {i > 0 && <span style={{ margin: '0 6px', color: colors.textMuted }}>/</span>}
                  {item.path && item.path !== '#' ? (
                    <a
                      href={item.path}
                      onClick={(e) => {
                        e.preventDefault();
                        navigate(item.path);
                      }}
                      style={{
                        color: i === breadcrumbItems.length - 1 ? colors.brandBlue : colors.textSecondary,
                        textDecoration: 'none',
                        fontWeight: i === breadcrumbItems.length - 1 ? 600 : 400,
                      }}
                    >
                      {i === 0 && (
                        <>
                          <HomeOutlined style={{ marginRight: 4 }} />
                        </>
                      )}
                      {item.title}
                    </a>
                  ) : (
                    <span
                      style={{
                        color: i === breadcrumbItems.length - 1 ? colors.brandBlue : colors.textSecondary,
                        fontWeight: i === breadcrumbItems.length - 1 ? 600 : 400,
                      }}
                    >
                      {i === 0 && (
                        <>
                          <HomeOutlined style={{ marginRight: 4 }} />
                        </>
                      )}
                      {item.title}
                    </span>
                  )}
                </span>
              ))}
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            <LocaleSwitcher />
            {/* TODO: 搜索和通知功能暂未实现，先隐藏 */}

            {user && (
              <Dropdown menu={userMenuItems} placement="bottomRight">
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    cursor: 'pointer',
                    padding: '4px 12px 4px 4px',
                    borderRadius: 10,
                  }}
                >
                  <div
                    style={{
                      width: 34,
                      height: 34,
                      borderRadius: 9,
                      background: `linear-gradient(135deg, ${colors.accent}, ${colors.brandBlue})`,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: '#fff',
                      fontSize: 14,
                      fontWeight: 600,
                    }}
                  >
                    {userDisplayName(user, t).charAt(0).toUpperCase()}
                  </div>
                  {!sidebarCollapsed && (
                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                      <span
                        style={{
                          fontSize: 14,
                          fontWeight: 500,
                          color: colors.textPrimary,
                        }}
                      >
                        {userDisplayName(user, t)}
                      </span>
                      <span style={{ fontSize: 12, color: colors.textMuted }}>{t('auth.admin')}</span>
                    </div>
                  )}
                </div>
              </Dropdown>
            )}
          </div>
        </header>

        {/* Content */}
        <main className="yx-content">{children}</main>
      </div>
    </div>
  );
}
