import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Spin, Result, Button, Table, Space, Tooltip, Modal, message } from 'antd';
import {
  CloudOutlined,
  FolderOpenOutlined,
  UploadOutlined,
  WifiOutlined,
  ApiOutlined,
  DatabaseOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { colors } from '@jonex/platform-theme/tokens';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { listDataAccessMethods, type DataAccessItem } from '../../api/dataAccess';
import {
  listConnections,
  readCatalog,
  testConnection,
  deleteConnection,
  type ConnectionItem,
} from '../../api/dataSourceConnection';
import ConnectionModal, { type ConnectionModalHandle } from './ConnectionModal';
import useCurrentSpace from '../../hooks/useCurrentSpace';
import './index.css';

const BUILT_IN_ACCESS_METHOD_IDS = new Set([
  'dam_demo_api',
  'dam_api_push_demo',
  'dam_demo_storage',
  'dam_demo_file',
  'dam_demo_mqtt',
  'dam_database',
]);

function getTypeIcons(
  t: (key: string, options?: Record<string, unknown>) => string,
): Record<string, { icon: React.ReactNode; label: string; desc: string }> {
  return {
    api: { icon: <CloudOutlined />, label: t('dataSource.apiAccess'), desc: t('dataSource.apiAccessDesc') },
    api_push: { icon: <ApiOutlined />, label: t('dataSource.apiPushAccess'), desc: t('dataSource.apiPushAccessDesc') },
    storage: {
      icon: <FolderOpenOutlined />,
      label: t('dataSource.storageAccess'),
      desc: t('dataSource.storageAccessDesc'),
    },
    file: {
      icon: <UploadOutlined />,
      label: t('dataSource.fileUploadAccess'),
      desc: t('dataSource.fileUploadAccessDesc'),
    },
    mqtt: { icon: <WifiOutlined />, label: t('dataSource.mqttAccess'), desc: t('dataSource.mqttAccessDesc') },
    database: {
      icon: <DatabaseOutlined />,
      label: t('dataSource.databaseAccess'),
      desc: t('dataSource.databaseAccessDesc'),
    },
  };
}

export default function DataAccess() {
  const { t } = useTranslation();
  const [items, setItems] = useState<DataAccessItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const typeIcons = React.useMemo(() => getTypeIcons(t), [t]);

  // ── 连接池（平台级，与上面的接入方式目录是两回事）──
  const modalRef = useRef<ConnectionModalHandle>(null);
  /** 连接按领域隔离：切换领域后列表必须换，不能沿用上一个领域的连接。 */
  const spaceId = useCurrentSpace();
  const [connections, setConnections] = useState<ConnectionItem[]>([]);
  const [connLoading, setConnLoading] = useState(false);
  /** 连接池自己的加载错误——空表 + 一句「暂无数据」会把「拉失败」说成「确实没有」 */
  const [connError, setConnError] = useState<string | null>(null);
  /** 行锁：正在读 catalog / 测连接的行**和动作** —— 别让同一行被连点。
   *  带上动作是为了只让被点的那颗按钮转圈（同一行两个动作共用一个锁，
   *  只判 id 的话两颗会一起转，看着像点错了）。 */
  const [busy, setBusy] = useState<{ id: string; action: 'catalog' | 'test' } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listDataAccessMethods(0, 100);
      setItems(result.items);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('dataSource.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  const reloadConnections = useCallback(async () => {
    if (!spaceId) {
      // 没有当前领域就不发请求：后端会拒，前端也不该把「未选领域」演成「一条都没有」。
      setConnections([]);
      setConnError(null);
      setConnLoading(false);
      return;
    }
    setConnLoading(true);
    setConnError(null);
    try {
      setConnections(await listConnections(spaceId));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setConnError(msg);
      message.error(msg);
    } finally {
      setConnLoading(false);
    }
  }, [spaceId]);

  useEffect(() => {
    reloadConnections();
  }, [reloadConnections]);

  /** 读 catalog 看表数。连接不通时后端给可读错误，直接弹出来。 */
  const onReadCatalog = useCallback(
    async (conn: ConnectionItem) => {
      if (!spaceId) return;
      setBusy({ id: conn.id, action: 'catalog' });
      try {
        const catalog = await readCatalog(conn.id, spaceId);
        Modal.info({
          title: t('dataSource.catalogTitle', { name: conn.name }),
          content: t('dataSource.catalogBody', {
            database: catalog.database,
            tables: catalog.stats.tables,
            columns: catalog.stats.columns,
            commented: catalog.stats.columns_with_comment,
          }),
          okText: t('common.confirm'),
        });
      } catch (err: unknown) {
        message.error(err instanceof Error ? err.message : t('dataSource.catalogFailed'));
      } finally {
        setBusy(null);
      }
    },
    [t, spaceId],
  );

  /** 测连接。**连不上库也是 HTTP 200 + `{ok:false,message}`**，不是异常 —— 看 `ok`，
   *  失败时把后端的 `message` 原样摊出来（认证失败、库不存在都在这条路上）。
   *
   *  为什么列表里要有这个按钮：创建时那条「建完立刻测」只覆盖**建的那一次**；
   *  凭据是会变的（密码轮换、库迁移），改完不重测就只剩「读 catalog」这一条
   *  间接路子 —— 而它回的是表数，说不清「连不上」和「这个库本来就没表」。 */
  const onTestConnection = useCallback(
    async (conn: ConnectionItem) => {
      if (!spaceId) return;
      setBusy({ id: conn.id, action: 'test' });
      try {
        const result = await testConnection(conn.id, spaceId);
        if (result.ok) {
          message.success([t('dataSource.testOk'), result.database, result.version].filter(Boolean).join(' · '));
        } else {
          message.error(t('dataSource.testFailedDetail', { message: result.message }));
        }
      } catch (err: unknown) {
        // 走不到「库连不上」：那被后端判成 200+ok:false。能到这里的只有传输层/权限/路由错误。
        message.error(err instanceof Error ? err.message : t('dataSource.testFailed'));
      } finally {
        setBusy(null);
      }
    },
    [t, spaceId],
  );

  const onDelete = useCallback(
    (conn: ConnectionItem) => {
      Modal.confirm({
        title: t('dataSource.deleteConnectionTitle'),
        content: t('dataSource.deleteConnectionContent', { name: conn.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        onOk: async () => {
          if (!spaceId) return;
          try {
            await deleteConnection(conn.id, spaceId);
            message.success(t('common.deleteSuccess'));
            reloadConnections();
          } catch (err: unknown) {
            // 仍被知识库引用时后端给 400 并列出库名 —— 原样展示，别吞掉
            message.error(err instanceof Error ? err.message : String(err));
          }
        },
      });
    },
    [t, reloadConnections, spaceId],
  );

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (error) {
    return (
      <Result
        status="error"
        title={t('dataSource.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <PageHeader title={t('dataSource.pageTitle')} />
      <PageContent showCard={false}>
        <div className="access-grid">
          {items.map((item) => {
            const cfg = typeIcons[item.access_type] || { icon: <CloudOutlined />, label: item.access_type, desc: '' };
            const isActive = item.status === 'active';
            const isBuiltIn = BUILT_IN_ACCESS_METHOD_IDS.has(item.id);

          return (
            <div key={item.id} className={`access-card${isActive ? ' active' : ' grey'}`}>
              <div className="icon-big">{cfg.icon}</div>
              <h3>{isBuiltIn ? cfg.label : item.name}</h3>
              <p>{isBuiltIn ? cfg.desc : item.description || cfg.desc}</p>
              <span className="status-tag">
                {isActive ? (
                  <>
                    <span className="dot-green" /> {t('dataSource.enabled')}
                  </>
                ) : (
                  t('dataSource.comingSoon')
                )}
              </span>
            </div>
          );
        })}
      </div>

      {/* ── 连接池：接入方式卡片下方的「实例」。
             按当前领域隔离；要接哪个库由同领域知识库设置页「添加数据源」时选。
             「生成 schema」**不在这里** —— schema 是每个知识库各自的，得在知识库那边生成。 ── */}
      <div style={{ marginTop: 32 }}>
        {!spaceId && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('dataSource.spaceRequired')}
          />
        )}
        <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, color: colors.brandDark, margin: 0 }}>
            {t('dataSource.connectionsTitle')}
          </h2>
          <Space>
            <Button icon={<ReloadOutlined />} loading={connLoading} onClick={reloadConnections}>
              {t('common.refresh')}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!spaceId}
              onClick={() => modalRef.current?.open()}
            >
              {t('dataSource.addDataSource')}
            </Button>
          </Space>
        </Space>

        <Table<ConnectionItem>
          rowKey="id"
          size="small"
          loading={connLoading}
          dataSource={connections}
          pagination={false}
          locale={{ emptyText: connError ?? t('dataSource.noData') }}
          columns={[
            { title: t('dataSource.name'), dataIndex: 'name', ellipsis: true },
            {
              title: t('dataSource.engine'),
              width: 200,
              render: (_: unknown, conn) => {
                const cfg = conn.config_json || {};
                return cfg.engine ? `${cfg.engine} / ${cfg.database ?? ''}` : '—';
              },
            },
            {
              title: t('dataSource.host'),
              width: 200,
              ellipsis: true,
              render: (_: unknown, conn) => {
                const cfg = conn.config_json || {};
                return cfg.host ? `${cfg.host}:${cfg.port ?? ''}` : '—';
              },
            },
            {
              title: t('dataSource.actions'),
              width: 340,
              render: (_: unknown, conn) => {
                // 这一行正在忙某件事 → 该动作那颗显示转圈，同行的其余按钮全部按死
                const rowBusy = busy?.id === conn.id;
                return (
                  <Space size={4}>
                    <Tooltip title={t('dataSource.testConnectionHint')}>
                      <Button
                        size="small"
                        loading={rowBusy && busy?.action === 'test'}
                        disabled={rowBusy && busy?.action !== 'test'}
                        onClick={() => onTestConnection(conn)}
                      >
                        {t('dataSource.testConnection')}
                      </Button>
                    </Tooltip>
                    <Tooltip title={t('dataSource.catalogHint')}>
                      <Button
                        size="small"
                        type="primary"
                        ghost
                        loading={rowBusy && busy?.action === 'catalog'}
                        disabled={rowBusy && busy?.action !== 'catalog'}
                        onClick={() => onReadCatalog(conn)}
                      >
                        {t('dataSource.readCatalog')}
                      </Button>
                    </Tooltip>
                    <Button size="small" disabled={rowBusy} onClick={() => modalRef.current?.open(conn)}>
                      {t('common.edit')}
                    </Button>
                    <Button size="small" danger disabled={rowBusy} onClick={() => onDelete(conn)}>
                      {t('common.delete')}
                    </Button>
                  </Space>
                );
              },
            },
          ]}
        />
      </div>

      <ConnectionModal ref={modalRef} spaceId={spaceId} onSaved={reloadConnections} />
      </PageContent>
    </>
  );
}
