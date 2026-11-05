import React, { useCallback, useState } from 'react';
import { Button, Drawer, Modal, Tooltip, message } from 'antd';
import { ApiOutlined, PlusOutlined, CloudOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import { deleteDataSource, generateDataSourceSchema, readDataSourceCatalog } from '@/api/dataSource';
import { dataSourceIconMap } from './index';
import type { DataSourceConfig } from '@/types/domainKnowledge';
import LineageTab from './lineage/LineageTab';
import EtlTab from './etl/EtlTab';

interface DataSourceTabProps {
  dataSources: DataSourceConfig[];
  dataSourcesLoading: boolean;
  /** KB 写权限（权限矩阵）：空间 owner/manager/租户管理员 或 授权 editor */
  canWrite?: boolean;
  /**
   * KB 的「结构化数据支持」开关。数据源页签恒定展示，但 database 源上的
   * 生成/编辑 schema、血缘、ETL 属结构化派生能力 —— 关闭时后端回 1004，
   * 这里干脆不给出会立刻报错的入口。
   */
  structuredEnabled?: boolean;
  onAdd: () => void;
  onEdit: (ds: DataSourceConfig) => void;
  onReload: () => void;
}

export default function DataSourceTab({
  dataSources,
  dataSourcesLoading,
  canWrite = false,
  structuredEnabled = true,
  onAdd,
  onEdit,
  onReload,
}: DataSourceTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // 本页挂在 `domain-knowledge/:id/detail` 下，kbId 从路由取——
  // DataSourceConfig 视图模型只有数据源 id，不带 knowledgeBaseId
  const { id: kbId = '' } = useParams<{ id: string }>();

  /** 真的在跑生成器的数据源 id —— 只有它配把按钮文案改成「加载中…」 */
  const [generating, setGenerating] = useState<string>();
  /**
   * 已发起、但还轮不到生成器的数据源 id：读 catalog + 等用户在确认框上点确定这一段。
   * 单独一个状态只为防连点（这一段用户可能连点两次、弹出两个确认框），**不驱动文案**——
   * 确认框开着的时候根本没在干活，挂个「加载中…」是骗人。
   */
  const [preparing, setPreparing] = useState<string>();
  /** 数据源卡片上的「血缘 / ETL」抽屉：固定到该源。 */
  const [panel, setPanel] = useState<
    { type: 'lineage' | 'etl'; ds: DataSourceConfig } | null>(null);

  /**
   * 生成该库自己那份 schema。
   *
   * 先读一次 catalog 再生成——只为把「这个库有多少表」摆在确认框里；
   * 读不出来（连不上、引擎不支持）就直接拦在这里，不让它白跑一趟生成器。
   */
  const onGenerate = useCallback(
    async (dsId: string, dsName: string) => {
      // 按行防连点：别的行照旧可以同时生成（文案状态只有一个，跨行并发本来就不准）
      if (preparing === dsId || generating === dsId) return;
      setPreparing(dsId);
      try {
        const catalog = await readDataSourceCatalog(dsId);
        const confirmed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            title: t('domainKnowledge.generateSchemaTitle', { name: dsName }),
            content: t('domainKnowledge.generateSchemaBody', {
              tables: catalog.stats.tables,
              columns: catalog.stats.columns,
              commented: catalog.stats.columns_with_comment,
            }),
            okText: t('domainKnowledge.generateSchema'),
            cancelText: t('common.cancelText'),
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) return;

        // 用户点了确定，从这一刻起才是真的在干活，按钮文案才可以变成「加载中…」
        setGenerating(dsId);
        const result = await generateDataSourceSchema(dsId);
        const entities = result.stats?.entities;
        const relations = result.stats?.relations;
        // 统计来自**外部**生成器的回包，字段名不归我们定。数字拿不齐就一个字都不编，
        // 退成不带数字的成功文案：这次生成是真真切切整份替换了本知识库已有的 schema，
        // 此刻报「0 个实体」比报不出来更坏——那是一句用户没法察觉的假话。
        if (typeof entities === 'number' && typeof relations === 'number') {
          message.success(t('domainKnowledge.generateSchemaOk', { entities, relations }));
        } else {
          message.success(t('domainKnowledge.generateSchemaOkNoStats'));
        }
        onReload();
      } catch (err: unknown) {
        // 生成失败（remote 模式的 ONTO_INTEL_BASE 不可达也走这里）——后端把原因写在 message 里了
        message.error(err instanceof Error ? err.message : t('domainKnowledge.generateSchemaFailed'));
      } finally {
        setPreparing(undefined);
        setGenerating(undefined);
      }
    },
    [t, onReload, preparing, generating],
  );

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <ApiOutlined className="yx-kb-icon-blue" /> {t('domainKnowledge.configuredDataSources')}
        </h3>
        <Button
          type="primary"
          className="yx-kb-section-add-btn"
          icon={<PlusOutlined />}
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={onAdd}
        >
          {t('domainKnowledge.addDataSource')}
        </Button>
      </div>
      {dataSourcesLoading ? (
        <div className="yx-kb-empty-state">{t('common.loading')}</div>
      ) : (
        dataSources.map((ds) => {
          const DsIcon = dataSourceIconMap[ds.iconType] || CloudOutlined;
          // database 源的卡片跳「它自己那份 schema」画布 —— 结构化关闭时那条路由
          // 上的接口会 1004，进来就是错误页，所以这里不放行跳转，只留编辑/删除。
          const schemaEntryBlocked = ds.accessType === 'database' && !structuredEnabled;
          return (
            <div
              key={ds.id}
              onClick={() => {
                if (schemaEntryBlocked) return;
                navigate(ds.path);
              }}
              className="yx-kb-ds-card"
              title={schemaEntryBlocked ? t('domainKnowledge.structuredRequiredHint') : undefined}
              style={schemaEntryBlocked ? { cursor: 'default' } : undefined}
              onMouseEnter={(e) => {
                if (schemaEntryBlocked) return;
                (e.currentTarget as HTMLElement).style.borderColor = '#dbe7f5';
                (e.currentTarget as HTMLElement).style.background = '#fafcff';
              }}
              onMouseLeave={(e) => {
                if (schemaEntryBlocked) return;
                (e.currentTarget as HTMLElement).style.borderColor = '#eef2f6';
                (e.currentTarget as HTMLElement).style.background = '#fff';
              }}
            >
              <div
                style={{
                  width: 40,
                  height: 40,
                  borderRadius: 8,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 18,
                  background: ds.iconBg,
                  color: ds.iconColor,
                  flexShrink: 0,
                }}
              >
                <DsIcon />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, fontWeight: 500, color: '#1e293b' }}>
                  {ds.name} <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 400 }}>· {ds.type}</span>
                </div>
                <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 2 }}>{ds.desc}</div>
              </div>
              {/* 数据库数据源独有：生成**该库自己那份** schema。
                  同一个连接被两个库引用时，两个库各生成各的、互不影响。 */}
              {ds.accessType === 'database' && structuredEnabled ? (
                <Tooltip title={t('domainKnowledge.generateSchemaHint')}>
                  <span
                    className="yx-kb-ds-link-edit"
                    style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!canWrite) return;
                      onGenerate(ds.id, ds.name);
                    }}
                  >
                    {generating === ds.id ? t('common.loading') : t('domainKnowledge.generateSchema')}
                  </span>
                </Tooltip>
              ) : null}
              {/* 数据库数据源独有：进图形页编辑它自己的那份 schema。
                  其他类型（api/storage/file）是文档来源，没有「库表结构」可编辑。 */}
              {ds.accessType === 'database' && structuredEnabled ? (
                <span
                  className="yx-kb-ds-link-edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    navigate(`/domain-knowledge/${kbId}/schema/${ds.id}`);
                  }}
                >
                  {t('domainKnowledge.editSchema')}
                </span>
              ) : null}
              {ds.accessType === 'database' && structuredEnabled ? (
                <span
                  className="yx-kb-ds-link-edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    setPanel({ type: 'lineage', ds });
                  }}
                >
                  {t('domainKnowledge.tab.lineage')}
                </span>
              ) : null}
              {ds.accessType === 'database' && structuredEnabled ? (
                <span
                  className="yx-kb-ds-link-edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    setPanel({ type: 'etl', ds });
                  }}
                >
                  {t('domainKnowledge.tab.etl')}
                </span>
              ) : null}
              <span
                className="yx-kb-ds-link-edit"
                style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!canWrite) return;
                  onEdit(ds);
                }}
              >
                {t('domainKnowledge.edit')}
              </span>
              <span
                className="yx-kb-ds-link-del"
                style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!canWrite) return;
                  Modal.confirm({
                    title: t('domainKnowledge.deleteDataSourceTitle'),
                    content: t('domainKnowledge.deleteDataSourceContent', { name: ds.name }),
                    okText: t('common.okText'),
                    cancelText: t('common.cancelText'),
                    okButtonProps: { danger: true },
                    onOk: async () => {
                      await deleteDataSource(ds.id);
                      message.success(t('common.deleteSuccess'));
                      onReload();
                    },
                  });
                }}
              >
                {t('domainKnowledge.delete')}
              </span>
            </div>
          );
        })
      )}
      <Drawer
        open={panel !== null}
        width={1080}
        destroyOnClose
        onClose={() => setPanel(null)}
        title={panel ? `${panel.ds.name} · ${t(panel.type === 'lineage'
          ? 'domainKnowledge.tab.lineage' : 'domainKnowledge.tab.etl')}` : ''}
      >
        {panel?.type === 'lineage' && (
          <LineageTab kbId={kbId} canWrite={canWrite}
                      fixedDataSourceId={panel.ds.id} />
        )}
        {panel?.type === 'etl' && (
          <EtlTab kbId={kbId} canWrite={canWrite}
                  targetDataSourceId={panel.ds.id} />
        )}
      </Drawer>
    </div>
  );
}
