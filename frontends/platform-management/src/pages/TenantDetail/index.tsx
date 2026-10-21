import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Result,
  Row,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { DescriptionsProps, TableProps } from 'antd';
import { EditOutlined, ReloadOutlined, SyncOutlined } from '@ant-design/icons';
import { readCachedUser, type ShellUser } from '@jonex/shell-sdk';
import { PageContent, PageHeader, PermButton, usePermission } from '@jonex/shared-lib';
import { TENANT_READ_PERMISSION, TENANT_WRITE_PERMISSION, getTenant, type TenantItem } from '../../api/tenants';
import {
  getConnectionStatusMeta,
  getIntegrationStatus,
  getIntervalLabel,
  getLoginPolicyLabel,
  getSyncStatusMeta,
  getTriggerLabel,
  listSyncRecords,
  type IntegrationStatusResponse,
  type SyncRecordSummary,
} from '../../api/identityIntegration';
import { fmtDateTime } from '../../utils/datetime';
import DisabledTip from '../../components/DisabledTip';
import TenantFormModal, { type TenantFormModalRef } from '../../components/TenantFormModal';
import SsoConfigDrawer from './SsoConfigDrawer';
import SyncConfigDrawer from './SyncConfigDrawer';
import SyncNowModal from './SyncNowModal';
import SyncRecordDrawer from './SyncRecordDrawer';
import './index.scss';

/**
 * 租户详情页。
 *
 * 基础信息以 `GET /platform/tenants/{id}` 为准：列表页经路由 state 携带的那一行只作首屏
 * 数据（避免 loading 闪烁），进页后仍会回读一次，刷新 / 深链 / 编辑后都能拿到最新值。
 *
 * ⚠️ 企业身份集成端点（E1–E6）的租户取自认证上下文（后端从 JWT 的 tenant_id 解析，
 * 与 X-Tenant-ID 不一致时直接报错），而本页可查看任意租户。两者不一致时不发请求，
 * 改为引导用户先切换租户，避免把当前登录租户的配置展示成目标租户的配置。
 *
 * 写链路：两张配置卡分别以 SSO / 同步抽屉承接 E2 保存与 E3 测试连接，
 * 「立即同步」以 SyncNowModal 承接 E4；成功后统一 `refreshAll()` 重拉。
 *
 * 详情（E6）由列表行的 `id` 打开；部分失败/失败时也会自动带出刚完成那次的失败明细。
 */
const { Text, Title } = Typography;

/** 响应式列数：宽屏 4 列（一行四个），窄屏降 2 / 1 */
const BASIC_COLUMNS: DescriptionsProps['column'] = { xs: 1, sm: 2, md: 2, lg: 4, xl: 4, xxl: 4 };
/** 身份模块子卡片内字段列数：固定一行一条信息，配合 .yx-desc-divided 的行间分隔线 */
const SUB_COLUMNS: DescriptionsProps['column'] = 1;

/** 同步记录分页大小（服务端分页，后端 page_size 上限 100） */
const RECORDS_PAGE_SIZE = 20;

/** 空值占位：一期 last_sync_at / next_sync_at 恒为 null，用短横线而非空白 */
const DASH = '—';

export default function TenantDetail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id = '' } = useParams<{ id: string }>();
  const location = useLocation();
  const user = readCachedUser<ShellUser>();
  // 权限来自 shell 下发的 permissions（hosted / standalone 两种形态都由 ShellContext 提供）
  const { permissions: userPermissions, hasPerm } = usePermission();
  const canRead = hasPerm(TENANT_READ_PERMISSION);
  // 比 PermButton 默认的「无权限」更具体：直接告诉用户差哪个码
  const writePermTip = t('tenantManagement.noPermission', { permission: TENANT_WRITE_PERMISSION });

  // ── 基础信息（GET /platform/tenants/{id}）──────────────────────────────
  // 列表页带过来的行数据作首屏值，随后的回读结果覆盖它
  const routeTenant = (() => {
    const state = (location.state ?? undefined) as { tenant?: TenantItem } | undefined;
    return state?.tenant && state.tenant.id === id ? state.tenant : undefined;
  })();

  const [tenant, setTenant] = useState<TenantItem | null>(routeTenant ?? null);
  const [tenantLoading, setTenantLoading] = useState(!routeTenant);
  const [tenantError, setTenantError] = useState<string | null>(null);
  const [tenantMissing, setTenantMissing] = useState(false);
  const tenantModalRef = useRef<TenantFormModalRef>(null);

  const loadTenant = useCallback(async () => {
    if (!id) return;
    setTenantLoading(true);
    setTenantError(null);
    try {
      setTenant(await getTenant(id));
      setTenantMissing(false);
    } catch (e: unknown) {
      // 404 = 租户不存在（或已被删除）；其余错误（403 / 网络）走可重试的错误态
      const status = (e as { status?: number } | null)?.status;
      if (status === 404) {
        setTenantMissing(true);
        setTenant(null);
      } else {
        setTenantError(e instanceof Error ? e.message : t('common.loadFailed'));
      }
    } finally {
      setTenantLoading(false);
    }
  }, [id, t]);

  // ── 企业身份集成（E1）+ 最近同步记录（E5）──────────────────────────────
  // 目标租户 = 本页租户；接口实际作用的租户 = 当前登录租户。不一致则整体降级为引导态。
  const currentTenantId = user?.tenantId ?? '';
  const tenantMatched = !!currentTenantId && currentTenantId === id;

  const [identity, setIdentity] = useState<IntegrationStatusResponse | null>(null);
  const [identityLoading, setIdentityLoading] = useState(true);
  const [identityError, setIdentityError] = useState<string | null>(null);

  const [records, setRecords] = useState<SyncRecordSummary[]>([]);
  const [recordsTotal, setRecordsTotal] = useState(0);
  const [recordsPage, setRecordsPage] = useState(1);
  const [recordsLoading, setRecordsLoading] = useState(true);
  const [recordsError, setRecordsError] = useState<string | null>(null);

  const loadIdentity = useCallback(async () => {
    setIdentityLoading(true);
    setIdentityError(null);
    try {
      setIdentity(await getIntegrationStatus());
    } catch (e: unknown) {
      setIdentityError(e instanceof Error ? e.message : t('tenantManagement.identity.loadFailed'));
    } finally {
      setIdentityLoading(false);
    }
  }, [t]);

  const loadRecords = useCallback(
    async (page: number) => {
      setRecordsLoading(true);
      setRecordsError(null);
      try {
        const r = await listSyncRecords(page, RECORDS_PAGE_SIZE);
        setRecords(r.items);
        setRecordsTotal(r.total);
      } catch (e: unknown) {
        setRecordsError(e instanceof Error ? e.message : t('common.loadFailed'));
      } finally {
        setRecordsLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    if (canRead) loadTenant();
  }, [canRead, loadTenant]);

  useEffect(() => {
    if (!canRead || !tenantMatched) {
      // 不请求也要结束 loading，否则引导态下骨架屏常驻
      setIdentityLoading(false);
      setRecordsLoading(false);
      return;
    }
    loadIdentity();
  }, [canRead, tenantMatched, loadIdentity]);

  useEffect(() => {
    if (!canRead || !tenantMatched) return;
    loadRecords(recordsPage);
  }, [canRead, tenantMatched, recordsPage, loadRecords]);

  /** 页头「刷新」与写链路（E2/E3/E4）成功后统一重拉，保证展示与后端一致 */
  const refreshAll = useCallback(() => {
    loadTenant();
    if (tenantMatched) {
      loadIdentity();
      loadRecords(recordsPage);
    }
  }, [loadTenant, loadIdentity, loadRecords, recordsPage, tenantMatched]);

  const [ssoOpen, setSsoOpen] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const [syncNowOpen, setSyncNowOpen] = useState(false);
  /** E6 详情所看的记录 ID：列表行取行内 `id`，同步结束后由 E4 的 `task_id` 带出。 */
  const [detailRecordId, setDetailRecordId] = useState<number | null>(null);

  if (!canRead) return <Result status="403" title="403" subTitle={t('error.403')} />;

  if (tenantMissing) {
    return (
      <Result
        status="404"
        title={t('tenantManagement.detail.notFoundTitle')}
        subTitle={t('tenantManagement.detail.notFoundDesc')}
        extra={
          <Button type="primary" onClick={() => navigate('/tenant-management')}>
            {t('tenantManagement.detail.backToList')}
          </Button>
        }
      />
    );
  }

  // 深链 / 刷新进入时没有路由 state，等详情接口返回前先占位
  if (!tenant) {
    return (
      <Flex vertical gap={20}>
        {tenantError ? (
          <Result
            status="error"
            title={t('common.loadFailed')}
            subTitle={tenantError}
            extra={
              <Button type="primary" onClick={loadTenant}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : (
          <Card>
            <Skeleton active paragraph={{ rows: 6 }} />
          </Card>
        )}
      </Flex>
    );
  }

  const dl = (key: string) => t(`tenantManagement.detail.${key}`);
  const di = (key: string, options?: Record<string, unknown>) => t(`tenantManagement.identity.${key}`, options);
  /** 同步抽屉/弹窗共用的一批文案（与 SyncNowModal / SyncConfigDrawer 的 dl 同命名空间） */
  const ds = (key: string) => t(`tenantManagement.syncDrawer.${key}`);

  /** 卡片内容三态：加载中骨架 / 失败可重试 / 正常内容 */
  const cardBody = (loading: boolean, error: string | null, onRetry: () => void, content: React.ReactNode) => {
    if (loading) return <Skeleton active title={false} paragraph={{ rows: 3 }} />;
    if (error) {
      return (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={onRetry}>
              {t('common.retry')}
            </Button>
          }
        />
      );
    }
    return content;
  };

  // 基本信息：一行四个（名称/ID/状态/备注）
  const basicItems: DescriptionsProps['items'] = [
    { key: 'name', label: dl('name'), children: <Text strong>{tenant.name}</Text> },
    { key: 'id', label: dl('id'), children: <Text code>{tenant.id}</Text> },
    {
      key: 'status',
      label: dl('status'),
      children:
        tenant.status === 1 ? (
          <Text type="success">{t('status.enabled')}</Text>
        ) : (
          <Text type="warning">{t('status.disabled')}</Text>
        ),
    },
    {
      key: 'remark',
      label: dl('remark'),
      children: tenant.description || <Text type="secondary">—</Text>,
    },
  ];

  // 单点登录：以现有页面字段为准（状态 / 身份标识规则 / 身份提供商 / 首次登录处理）
  const ssoItems: DescriptionsProps['items'] = identity
    ? [
        {
          key: 'sso-status',
          label: dl('ssoStatus'),
          // 启用与否是这一行的主结论，用标签而非彩字：启用绿、未启用红
          children: (
            <Tag color={identity.sso.enabled ? 'success' : 'error'}>
              {identity.sso.enabled ? dl('ssoEnabled') : dl('ssoDisabled')}
            </Tag>
          ),
        },
        {
          key: 'rule',
          label: dl('rule'),
          // 规则值即「外部用户标识 (sub)」，契约定为固定标识，不随字段映射变化
          children: dl('externalSubject'),
        },
        {
          key: 'provider',
          label: dl('provider'),
          children: identity.sso.provider === 'keycloak' ? 'Keycloak' : identity.sso.provider,
        },
        { key: 'firstLogin', label: dl('firstLogin'), children: getLoginPolicyLabel(t, identity.sso.login_policy) },
      ]
    : [];

  // 用户与组织同步：组织相关字段本期后置，不展示
  const datasource = identity?.datasource;
  const connection = datasource ? getConnectionStatusMeta(t, datasource.connection_status) : null;
  // 后端 connection_status === 'unconfigured' 即没有 datasource_url，此时同步必然失败，直接置灰
  const syncUnavailable = !datasource || datasource.connection_status === 'unconfigured';
  const syncItems: DescriptionsProps['items'] = datasource
    ? [
        { key: 'source', label: dl('syncSource'), children: datasource.name || DASH },
        {
          key: 'sync-status',
          label: dl('syncStatus'),
          children: datasource.auto_sync ? (
            <Text type="success">{dl('ssoEnabled')}</Text>
          ) : (
            <Text type="secondary">{dl('ssoDisabled')}</Text>
          ),
        },
        // 一期自动调度未落地，最近/下次同步时间恒为空，按占位展示
        {
          key: 'lastSync',
          label: dl('lastSync'),
          children: datasource.last_sync_at ? fmtDateTime(datasource.last_sync_at) : DASH,
        },
        {
          key: 'conn',
          label: dl('conn'),
          children: connection ? <Text type={connection.type}>{connection.label}</Text> : DASH,
        },
        { key: 'cycle', label: dl('cycle'), children: getIntervalLabel(t, datasource.interval) || DASH },
        {
          key: 'nextSync',
          label: dl('nextSync'),
          children: datasource.next_sync_at ? fmtDateTime(datasource.next_sync_at) : DASH,
        },
      ]
    : [];

  const columns: TableProps<SyncRecordSummary>['columns'] = [
    {
      title: dl('colTime'),
      dataIndex: 'sync_time',
      key: 'sync_time',
      width: 190,
      render: (v: string | null) => (v ? fmtDateTime(v) : DASH),
    },
    {
      title: dl('colTrigger'),
      dataIndex: 'trigger',
      key: 'trigger',
      width: 100,
      render: (v: SyncRecordSummary['trigger']) => getTriggerLabel(t, v),
    },
    {
      title: dl('colStatus'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status: SyncRecordSummary['status']) => {
        const meta = getSyncStatusMeta(t, status);
        return <Text type={meta.type}>{meta.label}</Text>;
      },
    },
    { title: dl('colChanged'), dataIndex: 'user_change', key: 'user_change', align: 'right', width: 130 },
    {
      title: dl('colFailed'),
      dataIndex: 'failed_count',
      key: 'failed_count',
      align: 'right',
      width: 110,
      render: (v: number) => (v > 0 ? <Text type="danger">{v}</Text> : <Text type="secondary">0</Text>),
    },
    {
      title: t('common.actions'),
      key: 'action',
      width: 110,
      // 成功行同样可点：失败明细为空态，用户变更三列仍有查看价值
      render: (_: unknown, r: SyncRecordSummary) => (
        <Button type="link" size="small" onClick={() => setDetailRecordId(r.id)}>
          {dl('viewRecord')}
        </Button>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        backTo={() => navigate('/tenant-management')}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}>
            {tenant.name}
            <Tag style={{ fontFamily: 'var(--yx-font-mono)' }}>{tenant.id}</Tag>
          </span>
        }
        extra={
          <Button icon={<ReloadOutlined />} loading={tenantLoading} onClick={refreshAll}>
            {t('common.refresh')}
          </Button>
        }
      />

      <PageContent showCard={false} style={{ gap: 20, minWidth: 0 }}>
        {/* 基本信息 */}
        <Card
          title={dl('basicTitle')}
          extra={
            <PermButton
              icon={<EditOutlined />}
              permissions={userPermissions}
              requiredPerm={TENANT_WRITE_PERMISSION}
              noPermissionText={writePermTip}
              onClick={() => tenantModalRef.current?.openEdit(tenant)}
            >
              {t('common.edit')}
            </PermButton>
          }
        >
          {/* 基本信息一行四格，两端对齐会让 value 与相邻格的 label 贴到一起，保持默认左对齐 */}
          <Descriptions layout="horizontal" column={BASIC_COLUMNS} items={basicItems} />
        </Card>

        {/* 企业身份集成：说明 + 左右两子卡 + 最近同步记录 */}
        <Flex vertical gap={16}>
          <Title level={5} style={{ margin: 0 }}>
            {dl('identityTitle')}
          </Title>

          {!tenantMatched ? (
            // 接口租户取自 JWT，与本页租户不一致时不能读取/写入，只给切换引导
            <Alert
              type="warning"
              showIcon
              message={di('tenantMismatchTitle')}
              description={di('tenantMismatchDesc', { current: currentTenantId || DASH, target: tenant.name })}
            />
          ) : (
            <>
              <Row gutter={[16, 16]}>
                {/* 单点登录 */}
                <Col xs={24} md={12} style={{ display: 'flex' }}>
                  <Card
                    style={{ flex: 1 }}
                    title={dl('ssoTitle')}
                    extra={
                      <PermButton
                        size="small"
                        permissions={userPermissions}
                        requiredPerm={TENANT_WRITE_PERMISSION}
                        noPermissionText={writePermTip}
                        onClick={() => setSsoOpen(true)}
                      >
                        {dl('editConfig')}
                      </PermButton>
                    }
                  >
                    {cardBody(
                      identityLoading,
                      identityError,
                      loadIdentity,
                      <Descriptions
                        className="yx-desc-divided yx-desc-value-right"
                        layout="horizontal"
                        column={SUB_COLUMNS}
                        items={ssoItems}
                      />,
                    )}
                  </Card>
                </Col>
                {/* 用户与组织同步 */}
                <Col xs={24} md={12} style={{ display: 'flex' }}>
                  <Card
                    style={{ flex: 1 }}
                    title={dl('syncTitle')}
                    extra={
                      <Space size={8}>
                        <DisabledTip tip={syncUnavailable ? dl('syncNeedConfig') : ''}>
                          <PermButton
                            size="small"
                            type="primary"
                            icon={<SyncOutlined />}
                            permissions={userPermissions}
                            requiredPerm={TENANT_WRITE_PERMISSION}
                            noPermissionText={writePermTip}
                            // 业务原因（未配置连接）比权限原因更具体，此时由外层 DisabledTip 出提示
                            noPermissionTip={!syncUnavailable}
                            disabled={syncUnavailable}
                            onClick={() => setSyncNowOpen(true)}
                          >
                            {dl('syncNow')}
                          </PermButton>
                        </DisabledTip>
                        <PermButton
                          size="small"
                          permissions={userPermissions}
                          requiredPerm={TENANT_WRITE_PERMISSION}
                          noPermissionText={writePermTip}
                          onClick={() => setSyncOpen(true)}
                        >
                          {dl('editConfig')}
                        </PermButton>
                      </Space>
                    }
                  >
                    {cardBody(
                      identityLoading,
                      identityError,
                      loadIdentity,
                      // 未配置数据源时，E1 除 connection_status 外全是空值，渲染出来只是一排「—」，
                      // 换成带配置入口的空态
                      syncUnavailable ? (
                        <Empty
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description={dl('syncEmpty')}
                          style={{ margin: '12px 0' }}
                        >
                          <PermButton
                            size="small"
                            permissions={userPermissions}
                            requiredPerm={TENANT_WRITE_PERMISSION}
                            noPermissionText={writePermTip}
                            onClick={() => setSyncOpen(true)}
                          >
                            {dl('editConfig')}
                          </PermButton>
                        </Empty>
                      ) : (
                        <>
                          {/* 波 3 闸门：未通过连接测试（有地址但 verified_at 为空）⇒ 自动同步已被
                            后端直接跳过。红字必须出在这里，否则「被停摆」这件事在界面上看不见。
                            判据与后端门控同源；空态（unconfigured）由上面的 Empty 分支承接，不到这。 */}
                          {datasource?.connection_status === 'unverified' ? (
                            <Text type="danger" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                              {ds('syncPausedHint')}
                            </Text>
                          ) : null}
                          <Descriptions
                            className="yx-desc-divided yx-desc-value-right"
                            layout="horizontal"
                            column={SUB_COLUMNS}
                            items={syncItems}
                          />
                        </>
                      ),
                    )}
                  </Card>
                </Col>
              </Row>

              {/* 最近同步记录 */}
              <Card title={dl('recordsTitle')}>
                {recordsError ? (
                  <Alert
                    type="error"
                    showIcon
                    message={recordsError}
                    style={{ marginBottom: records.length ? 16 : 0 }}
                    action={
                      <Button size="small" onClick={() => loadRecords(recordsPage)}>
                        {t('common.retry')}
                      </Button>
                    }
                  />
                ) : null}
                <Table<SyncRecordSummary>
                  columns={columns}
                  dataSource={records}
                  rowKey="id"
                  loading={recordsLoading}
                  size="middle"
                  locale={{ emptyText: t('tenantManagement.detail.recordsEmpty') }}
                  pagination={{
                    current: recordsPage,
                    pageSize: RECORDS_PAGE_SIZE,
                    total: recordsTotal,
                    showSizeChanger: false,
                    showTotal: (total) => t('common.totalPage', { total }),
                    onChange: setRecordsPage,
                  }}
                />
              </Card>
            </>
          )}
        </Flex>
      </PageContent>

      <TenantFormModal ref={tenantModalRef} onSaved={loadTenant} />

      <SsoConfigDrawer
        open={ssoOpen}
        status={identity?.sso ?? null}
        onClose={() => setSsoOpen(false)}
        onChanged={refreshAll}
      />
      <SyncConfigDrawer
        open={syncOpen}
        status={identity?.datasource ?? null}
        onClose={() => setSyncOpen(false)}
        onChanged={refreshAll}
      />
      <SyncNowModal
        open={syncNowOpen}
        // 波 3 闸门：未测通的租户在弹窗内二次确认（卡片按钮不置灰，拦截点全在弹窗里）
        unverified={datasource?.connection_status === 'unverified'}
        onClose={() => setSyncNowOpen(false)}
        onDone={(res) => {
          refreshAll();
          // 失败/部分失败时直接带出失败明细，省去用户再找入口
          if (res.status === 'failed' || res.status === 'partial_failed') setDetailRecordId(res.task_id);
        }}
        // 「同步进行中」冲突下本次没有结果可带，但视图已过期，同样要重拉
        onConflict={refreshAll}
      />
      <SyncRecordDrawer recordId={detailRecordId} onClose={() => setDetailRecordId(null)} />
    </>
  );
}
