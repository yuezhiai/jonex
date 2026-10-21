import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Result, Space, Table, Tag, message } from 'antd';
import type { TableProps } from 'antd';
import { DeleteOutlined, EditOutlined, EyeOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { readCachedUser, type ShellUser } from '@jonex/shell-sdk';
import { PageContent, PageHeader, PermButton, usePermission } from '@jonex/shared-lib';
import {
  TENANT_READ_PERMISSION,
  TENANT_WRITE_PERMISSION,
  deleteTenant,
  listTenants,
  type TenantItem,
} from '../../api/tenants';
import DisabledTip from '../../components/DisabledTip';
import TenantFormModal, { type TenantFormModalRef } from '../../components/TenantFormModal';

/** 输入防抖：避免每敲一个字符就拉一页数据 */
const SEARCH_DEBOUNCE_MS = 300;
const PAGE_SIZE_DEFAULT = 10;

export default function TenantManagement() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = readCachedUser<ShellUser>();
  // 权限来自 shell 下发的 permissions（hosted / standalone 两种形态都由 ShellContext 提供）
  const { permissions: userPermissions, hasPerm } = usePermission();
  const canRead = hasPerm(TENANT_READ_PERMISSION);
  // 比 PermButton 默认的「无权限」更具体：直接告诉用户差哪个码
  const writePermTip = t('tenantManagement.noPermission', { permission: TENANT_WRITE_PERMISSION });

  const [tenants, setTenants] = useState<TenantItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE_DEFAULT);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const modalRef = useRef<TenantFormModalRef>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 关键字交给后端（ID / 名称 / 描述模糊匹配），分页与 total 都由后端算 —— 本地过滤只看得到当前页
      const r = await listTenants(page, pageSize, searchTerm);
      setTenants(r.items);
      setTotal(r.total);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, searchTerm, t]);

  useEffect(() => {
    if (canRead) load();
  }, [load, canRead]);

  // 输入防抖；页码与关键字在同一次批处理里更新，切入/切出搜索态只触发一次请求
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearchTerm(search.trim());
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const openCreate = () => modalRef.current?.openCreate();
  const openEdit = (item: TenantItem) => modalRef.current?.openEdit(item);
  const openDetail = (item: TenantItem) =>
    navigate(`/tenant-management/${encodeURIComponent(item.id)}`, { state: { tenant: item } });

  const handleDelete = (item: TenantItem) => {
    Modal.confirm({
      title: t('tenantManagement.deleteConfirmTitle'),
      content: t('tenantManagement.deleteConfirmDesc', { name: item.name, id: item.id }),
      okText: t('common.confirm'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await deleteTenant(item.id);
          message.success(t('tenantManagement.deleted'));
          // 删掉当前页最后一条时回退一页，避免停在空页
          if (tenants.length === 1 && page > 1) setPage(page - 1);
          else load();
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : t('tenantManagement.deleteFailed'));
        }
      },
    });
  };

  const columns: TableProps<TenantItem>['columns'] = [
    {
      title: t('tenantManagement.tenantId'),
      dataIndex: 'id',
      key: 'id',
      width: 180,
      render: (id: string, tenant: TenantItem) => (
        <a className="yx-table-action" onClick={() => openDetail(tenant)}>
          {id}
        </a>
      ),
    },
    {
      // [jonex] 2026-10-15 名称/描述一律显示库里的原值。此前这里过 tenantDisplay()，
      // 会把三个内置租户（tenant_jonex_{demo,alpha,beta}）的值换成 i18n 文案，
      // 造成界面与库不一致（改个描述还会让名称跟着跳）；该层已整体去掉。
      title: t('tenantManagement.name'),
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: t('tenantManagement.description'),
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: number) => (
        <Tag color={v === 1 ? 'success' : 'warning'}>{v === 1 ? t('status.enabled') : t('status.disabled')}</Tag>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      // 三个带图标的 link 按钮，比原 `.yx-table-action` 的裸文本宽
      width: 240,
      render: (_: unknown, r: TenantItem) => {
        // 删除自己所属的租户会把当前登录会话所在租户一并软删，直接置灰
        const isSelf = r.id === user?.tenantId;
        return (
          <Space size={0}>
            {/* 能进本页即已通过 read 权限的路由守卫，查看不再单独判权 */}
            <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => openDetail(r)}>
              {t('tenantManagement.view')}
            </Button>
            <PermButton
              type="link"
              size="small"
              icon={<EditOutlined />}
              permissions={userPermissions}
              requiredPerm={TENANT_WRITE_PERMISSION}
              noPermissionText={writePermTip}
              onClick={() => openEdit(r)}
            >
              {t('common.edit')}
            </PermButton>
            <DisabledTip tip={isSelf ? t('tenantManagement.deleteSelfTip') : ''}>
              <PermButton
                type="link"
                size="small"
                danger
                icon={<DeleteOutlined />}
                permissions={userPermissions}
                requiredPerm={TENANT_WRITE_PERMISSION}
                noPermissionText={writePermTip}
                // 业务原因（本租户不可删）比权限原因更具体，此时由外层 DisabledTip 出提示
                noPermissionTip={!isSelf}
                disabled={isSelf}
                onClick={() => handleDelete(r)}
              >
                {t('common.delete')}
              </PermButton>
            </DisabledTip>
          </Space>
        );
      },
    },
  ];

  if (!canRead) return <Result status="403" title="403" subTitle={t('error.403')} />;

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
    <>
      <PageHeader title={t('tenantManagement.title')} />
      <PageContent showCard={false}>
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
            <PermButton
              type="primary"
              icon={<PlusOutlined />}
              permissions={userPermissions}
              requiredPerm={TENANT_WRITE_PERMISSION}
              noPermissionText={writePermTip}
              onClick={openCreate}
            >
              {t('tenantManagement.createTenant')}
            </PermButton>
          </div>
          <Table
            columns={columns}
            dataSource={tenants}
            loading={loading}
            rowKey="id"
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              pageSizeOptions: ['10', '20', '50'],
              onChange: (p, ps) => {
                setPage(p);
                setPageSize(ps);
              },
            }}
            size="middle"
          />
        </div>

        <TenantFormModal ref={modalRef} onSaved={load} />
      </PageContent>
    </>
  );
}
