import React, { useState, useEffect, useMemo } from 'react';
import { Layout, Menu, Button, Dropdown, Drawer, Avatar, Typography } from 'antd';
import { MenuOutlined, ArrowLeftOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate, useLocation, useMatches } from 'react-router-dom';
import { useStore } from '@/store';
import { usePermission } from '@jonex/shared-lib';
import { findMenuPathKeys, getMenuItemsByPermission } from '@/utils/menu';
import { getAvatarText } from '@/utils/utils';
import { buildLoginRedirectUrl, clearAuthStorage } from '@jonex/shell-sdk';
import { safeMessage } from '@/utils/safeMessage';
import useIsMobile from '@/hooks/useIsMobile';
import { getMenuFromRoutes } from '@/router/menu';
import { getRoutes } from '@/router/routes.config';
import styles from './index.module.scss';

const { Text, Title } = Typography;
const { Header } = Layout;

const VITE_LOGIN = import.meta.env.VITE_LOGIN || '';
const VITE_APP_ID = import.meta.env.VITE_APP_ID || 'core-business';

interface HeaderNavProps {
  type?: string | null;
  previous?: string;
  title?: string;
  previousTitle?: string;
}

export default function HeaderNav({ type = null, previous = '', title = '', previousTitle = '' }: HeaderNavProps) {
  const { global } = useStore();
  const userInfo = global?.userInfo as Record<string, any> | null | undefined;
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const matches = useMatches();
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const isMobile = useIsMobile((mobile: boolean) => {
    if (!mobile) {
      setDrawerOpen(false);
    }
  });
  const { permissions: userPermissions } = usePermission();
  const menuItems = useMemo(
    () => getMenuItemsByPermission(getMenuFromRoutes(getRoutes(), t), userPermissions),
    [t, userPermissions],
  );

  const rolesOptions = {
    admin: t('auth.admin'),
    user: t('auth.user'),
  };

  const currentTitle =
    ((
      matches.reverse().find((match) => match.handle && (match.handle as Record<string, unknown>).title)
        ?.handle as Record<string, unknown>
    )?.title as string) || '';

  useEffect(() => {
    if (currentTitle) {
      document.title = t(currentTitle);
    } else {
      document.title = t('site.title');
    }
  }, [currentTitle, i18n.language]);

  useEffect(() => {
    const pathname = location.pathname;
    const { selectedKey } = findMenuPathKeys(getMenuFromRoutes(getRoutes(), t), pathname);
    setSelectedKeys(selectedKey ? [selectedKey] : []);
  }, [location.pathname]);

  const handleMenuClick = ({ key }: { key: string }) => {
    navigate(key);
    setDrawerOpen(false);
  };

  const handleUserMenuClick = () => {
    const shellContext = (window as any).__SHELL_CONTEXT__;
    if (shellContext?.mode === 'hosted' && typeof shellContext.logout === 'function') {
      shellContext.logout();
      return;
    }
    clearAuthStorage({ keepLocale: true });
    global.setUserInfo(null);
    const loginUrl = VITE_LOGIN || '/login';
    window.location.href = buildLoginRedirectUrl(loginUrl, window.location.href, VITE_APP_ID);
  };

  const renderMenuContent = () => (
    <div className={styles.left}>
      <Title level={3} className={styles.logo} onClick={() => navigate('/')}>
        {t('site.title')}
      </Title>

      {!isMobile ? (
        menuItems?.length > 0 ? (
          <Menu
            mode="horizontal"
            className={styles.menuBox}
            items={menuItems as any}
            selectable
            selectedKeys={selectedKeys}
            onClick={handleMenuClick}
          />
        ) : null
      ) : (
        <Button type="text" icon={<MenuOutlined />} onClick={() => setDrawerOpen(true)} />
      )}
    </div>
  );

  const renderBackContent = () => (
    <div className={styles.left}>
      <Button
        color="primary"
        variant="text"
        type="link"
        className={styles.btnBack}
        icon={<ArrowLeftOutlined />}
        onClick={() => navigate(-1)}
      >
        {t('common.backTo', { title: t(previousTitle) })}
      </Button>
      <Title level={3} className={styles.title}>
        {t(title)}
      </Title>
    </div>
  );

  const AvatarIcon = () => (
    <Avatar className={styles.avatar} size="large" gap={4} src={userInfo?.avatar || undefined}>
      {!userInfo?.avatar && (getAvatarText(userInfo?.realName) || '')}
    </Avatar>
  );

  const renderUserDropdown = () => (
    <div className={styles.dropdownRenderBox}>
      <div className={styles.topBox}>
        {AvatarIcon()}

        <div className={styles.userBox}>
          <Text ellipsis={{ tooltip: true }} className={styles.ellipsis} style={{ display: 'block' }}>
            {userInfo?.email || ''}
          </Text>
          <Text ellipsis={{ tooltip: true }} className={styles.ellipsis} style={{ display: 'block' }}>
            {userInfo?.realName || ''}
          </Text>
          <Text type="secondary" ellipsis={{ tooltip: true }} className={styles.ellipsis} style={{ display: 'block' }}>
            {(Array.isArray(userInfo?.roles)
              ? userInfo.roles
              : String(userInfo?.roles || '')
                  .split(',')
                  .filter(Boolean)
            )
              .map((role: string) => (rolesOptions as any)[role.trim()] || role)
              .join('，')}
          </Text>
        </div>
      </div>

      <div style={{ borderTop: '1px solid rgba(31, 35, 41, 0.15)' }}>
        <div style={{ padding: '3px' }}>
          <div className={styles.logoutBox} onClick={handleUserMenuClick}>
            {t('auth.signOut')}
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <Header className={`${styles.header} ${type === 'back' && styles.headerBack}`}>
      {type === 'back' ? renderBackContent() : renderMenuContent()}

      <div className={styles.right}>
        <Dropdown popupRender={renderUserDropdown}>{AvatarIcon()}</Dropdown>
      </div>

      <Drawer placement="left" styles={{ body: { width: 280 } }} open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        <Menu mode="inline" items={menuItems as any} selectable selectedKeys={selectedKeys} onClick={handleMenuClick} />
      </Drawer>
    </Header>
  );
}
