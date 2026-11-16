import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  RobotOutlined,
  TeamOutlined,
  UserOutlined,
  SafetyOutlined,
  ScheduleOutlined,
  FileTextOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { EntryGrid, PageContent, PageHeader } from '@jonex/shared-lib';
import type { EntryGridItem } from '@jonex/shared-lib';

function getActions(t: (key: string) => string): EntryGridItem[] {
  return [
    {
      key: '/model-adapter',
      label: t('navigation.modelAdapter'),
      description: t('platform.homeCardModelAdapterDesc'),
      path: '/model-adapter',
      icon: <RobotOutlined />,
      color: '#10b981',
    },
    {
      key: '/tenant-management',
      label: t('navigation.tenantManagement'),
      description: t('platform.homeCardTenantManagementDesc'),
      path: '/tenant-management',
      icon: <TeamOutlined />,
      color: 'var(--yx-accent)',
    },
    {
      key: '/user-management',
      label: t('navigation.userManagement'),
      description: t('platform.homeCardUserManagementDesc'),
      path: '/user-management',
      icon: <UserOutlined />,
      color: '#8b5cf6',
    },
    {
      key: '/role-permission',
      label: t('navigation.rolePermission'),
      description: t('platform.homeCardRolePermissionDesc'),
      path: '/role-permission',
      icon: <SafetyOutlined />,
      color: '#f59e0b',
    },
    {
      key: '/task-schedule',
      label: t('navigation.taskSchedule'),
      description: t('platform.homeCardTaskScheduleDesc'),
      path: '/task-schedule',
      icon: <ScheduleOutlined />,
      color: '#ec4899',
    },
    {
      key: '/system-config',
      label: t('navigation.systemConfig'),
      description: t('platform.homeCardSystemConfigDesc'),
      path: '/system-config',
      icon: <SettingOutlined />,
      color: 'var(--yx-text-secondary)',
    },
    {
      key: '/operation-log',
      label: t('navigation.operationLog'),
      description: t('platform.homeCardOperationLogDesc'),
      path: '/operation-log',
      icon: <FileTextOutlined />,
      color: '#ef4444',
    },
  ];
}

export default function Home() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t('platform.homeTitle')} subtitle={t('platform.homeSubtitle')} />
      <PageContent>
        <EntryGrid items={getActions(t)} onNavigate={navigate} />
      </PageContent>
    </>
  );
}
