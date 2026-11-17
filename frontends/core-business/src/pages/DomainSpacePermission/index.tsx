import React from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Card, Table, Button } from 'antd';
import { SearchOutlined, PlusOutlined, CheckCircleFilled, CloseCircleFilled } from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';

const permissions = [
  { roleKey: 'domainPermission.roleAdmin', read: true, write: true, manage: true, delete: true },
  { roleKey: 'domainPermission.roleEditor', read: true, write: true, manage: false, delete: false },
  { roleKey: 'domainPermission.roleViewer', read: true, write: false, manage: false, delete: false },
  { roleKey: 'domainPermission.roleGuest', read: true, write: false, manage: false, delete: false },
];

export default function DomainSpacePermission() {
  const { t } = useTranslation();
  const permissionRows = permissions.map((item) => ({ ...item, role: t(item.roleKey) }));
  const columns = [
    { title: t('domainPermission.columnRole'), dataIndex: 'role', key: 'role', width: 120 },
    {
      title: t('domainPermission.columnRead'),
      dataIndex: 'read',
      key: 'read',
      width: 80,
      render: (v: boolean) =>
        v ? <CheckCircleFilled style={{ color: '#059669' }} /> : <CloseCircleFilled style={{ color: 'var(--yx-error-text)' }} />,
    },
    {
      title: t('domainPermission.columnWrite'),
      dataIndex: 'write',
      key: 'write',
      width: 80,
      render: (v: boolean) =>
        v ? <CheckCircleFilled style={{ color: '#059669' }} /> : <CloseCircleFilled style={{ color: 'var(--yx-error-text)' }} />,
    },
    {
      title: t('domainPermission.columnManage'),
      dataIndex: 'manage',
      key: 'manage',
      width: 80,
      render: (v: boolean) =>
        v ? <CheckCircleFilled style={{ color: '#059669' }} /> : <CloseCircleFilled style={{ color: 'var(--yx-error-text)' }} />,
    },
    {
      title: t('domainPermission.columnDelete'),
      dataIndex: 'delete',
      key: 'delete',
      width: 80,
      render: (v: boolean) =>
        v ? <CheckCircleFilled style={{ color: '#059669' }} /> : <CloseCircleFilled style={{ color: 'var(--yx-error-text)' }} />,
    },
    {
      title: t('domainPermission.columnActions'),
      key: 'actions',
      width: 80,
      render: () => <a className="yx-table-action">{t('common.edit')}</a>,
    },
  ];
  return (
    <>
      <PageHeader title={t('domainPermission.title')} />
      <PageContent showCard={false}>
        <Card className="yx-card">
          <div className="yx-toolbar">
            <Input
              prefix={<SearchOutlined />}
              placeholder={t('domainPermission.searchPlaceholder')}
              style={{ width: 240 }}
            />
            <Button type="primary" icon={<PlusOutlined />}>
              {t('domainPermission.addRole')}
            </Button>
          </div>
          <Table columns={columns} dataSource={permissionRows} rowKey="roleKey" pagination={false} size="middle" />
          <div
            style={{
              marginTop: 16,
              padding: '10px 14px',
              background: 'var(--yx-info-bg)',
              borderRadius: 8,
              fontSize: 13,
              color: 'var(--yx-text-secondary)',
            }}
          >
            {t('domainPermission.noteText')}
          </div>
        </Card>
      </PageContent>
    </>
  );
}
