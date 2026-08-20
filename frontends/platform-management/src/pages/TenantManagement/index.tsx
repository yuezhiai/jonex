import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Input, Button, Table, Tag, Spin, Result } from 'antd';
import { SearchOutlined, PlusOutlined, EditOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import { listTenants, type TenantItem } from '../../api/tenants';
import { tenantDisplay } from '../../utils/tenantDisplay';
import TenantFormModal, { type TenantFormModalRef } from './TenantFormModal';

export default function TenantManagement() {
  const { t } = useTranslation();
  const user = readCachedUser<ShellUser>();
  const isAdmin = isPlatformAdmin(user);
  const [tenants, setTenants] = useState<TenantItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const modalRef = useRef<TenantFormModalRef>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await listTenants();
      setTenants(r.items);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (isAdmin) load();
  }, [load, isAdmin]);

  const filtered = tenants.filter((tenant) => {
    const display = tenantDisplay(tenant, t);
    return (
      !search ||
      `${display.name} ${display.description} ${tenant.name} ${tenant.id}`.toLowerCase().includes(search.toLowerCase())
    );
  });

  const openCreate = () => modalRef.current?.openCreate();
  const openEdit = (item: TenantItem) => modalRef.current?.openEdit(item);

  const planLabel = (v: string) => {
    if (v === 'free') return t('tenantManagement.planFree');
    if (v === 'pro') return t('tenantManagement.planPro');
    if (v === 'enterprise') return t('tenantManagement.planEnterprise');
    return v;
  };

  const columns = [
    { title: t('tenantManagement.tenantId'), dataIndex: 'id', key: 'id', width: 180 },
    {
      title: t('tenantManagement.name'),
      dataIndex: 'name',
      key: 'name',
      render: (_: string, tenant: TenantItem) => <a className="yx-table-action">{tenantDisplay(tenant, t).name}</a>,
    },
    {
      title: t('tenantManagement.description'),
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (_: string | null, tenant: TenantItem) => tenantDisplay(tenant, t).description,
    },
    {
      title: t('tenantManagement.plan'),
      dataIndex: 'plan_type',
      key: 'plan_type',
      width: 80,
      render: (v: string) => planLabel(v),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 80,
      render: (v: number) => (
        <Tag color={v === 1 ? 'success' : 'warning'}>{v === 1 ? t('status.enabled') : t('status.disabled')}</Tag>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, r: TenantItem) => (
        <a className="yx-table-action" onClick={() => openEdit(r)}>
          <EditOutlined /> {t('common.edit')}
        </a>
      ),
    },
  ];

  if (!isAdmin)
    return (
      <Result status="403" title="403" subTitle={t('error.403')} />
    );

  if (error)
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('tenantManagement.title')}</h1>
      </div>
      <div className="yx-card">
        <div className="yx-toolbar">
          <Input
            prefix={<SearchOutlined />}
            placeholder={t('tenantManagement.searchTenants')}
            style={{ width: 240 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('tenantManagement.createTenant')}
          </Button>
        </div>
        <Table
          columns={columns}
          dataSource={filtered}
          loading={loading}
          rowKey="id"
          pagination={{ total: filtered.length, pageSize: 10 }}
          size="middle"
        />
      </div>

      <TenantFormModal ref={modalRef} onSaved={load} />
    </div>
  );
}
