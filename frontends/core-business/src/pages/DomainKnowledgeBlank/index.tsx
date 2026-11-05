import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button, Result, Skeleton, Space, Tabs } from 'antd';
import {
  FileTextOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  BuildOutlined,
  SettingOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { PageContent, PageHeader, getErrorMessage } from '@jonex/shared-lib';
import type { DomainKnowledgeDetail, DocumentStatsResult, WikiStats } from '@/types/domainKnowledge';
import { getDomainKnowledgeDetail, getDocumentStats, getWikiContents } from '@/api/domainKnowledge';
import { listDataSources } from '@/api/dataSource';
import { dataSourceInstanceDisplayName } from '@/utils/dataSourceDisplay';
import { isLlmWikiType } from '@/utils/kbType';
import { useQuota } from '@/hooks/useQuota';
import AssetStatTag, { buildAssetTags } from '@/components/AssetStatTag';
import CompileAssetsPanel from '@/pages/DomainKnowledgeCompileResults/CompileAssetsPanel';
import KnowledgeTracking from '@/pages/KnowledgeTracking';
import DocumentLibrary from './DocumentLibrary';
import './index.scss';

// tab key 同时是 URL 上的 ?tab= 取值；documents 是默认值，不写进地址栏
const TAB_DOCUMENTS = 'documents';
const TAB_ASSETS = 'assets';
const TAB_TRACKING = 'tracking';
// 白名单：?tab= 只认这三个值，其余（含被篡改的）一律落回默认页
const TAB_KEYS: string[] = [TAB_DOCUMENTS, TAB_ASSETS, TAB_TRACKING];

export default function DomainKnowledgeBlank() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id = '' } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [detail, setDetail] = useState<DomainKnowledgeDetail | null>(null);
  // 详情首次加载失败（典型是密级守卫 404 / code=1002）：整页报错而不是留骨架屏。
  // 已有 detail 时刷新失败仍走「保留旧值」，不清空页头。
  const [detailError, setDetailError] = useState<string | null>(null);
  const [dataSources, setDataSources] = useState<string>('—');
  const [docStats, setDocStats] = useState<DocumentStatsResult | null>(null);
  // 每次切到编译资产 tab 都 bump → 面板重挂载 → 统计数据与子表全部重新拉取。
  // 编译是异步的，切回来往往就是想看最新结果。不用 destroyInactiveTabPane：
  // 那个是全局开关，会连带销毁文档库 pane 的分页、筛选、上传状态。
  const [assetsMountKey, setAssetsMountKey] = useState(0);
  // Wiki 主题/概念不在 KB 详情里，得单独取全库 Wiki 内容（document_id 空 = KB 级）。
  // null 表示「还没拿到」而不是「没有」——页头标签据此决定要不要成组出现。
  const [wikiStats, setWikiStats] = useState<WikiStats | null>(null);

  // 文档配额（knowledgeBaseDocumentLimit，传 kb_id 取该 KB 真实 used；加载失败展示 '—'）
  // 提升在父级：DocumentLibrary 刷新列表时经 onRefreshQuota 同步刷新，统计栏与上传按钮共用同一份数据
  const { getQuota, isReached, error: quotaError, reload: reloadQuota } = useQuota(id);
  const docQuota = getQuota('knowledgeBaseDocumentLimit');
  const docReached = isReached('knowledgeBaseDocumentLimit');

  // tab 选中态与 ?tab= 同步：白名单式取值（非法值落回默认），默认值不写进 URL。
  // 放 URL 而非本地 state，是为了刷新页面、复制链接后仍停在同一 tab。
  const tabParam = searchParams.get('tab') ?? '';
  const activeTab = TAB_KEYS.includes(tabParam) ? tabParam : TAB_DOCUMENTS;

  const handleTabChange = useCallback(
    (key: string) => {
      if (key === TAB_ASSETS) setAssetsMountKey((n) => n + 1);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (key === TAB_DOCUMENTS) next.delete('tab');
          else next.set('tab', key);
          return next;
        },
        // replace：切 tab 不该在浏览器历史里堆一摞记录，返回键要能直接离开本页
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // 带上来源：设置页据此把「返回」指回本页，而不是知识库列表
  const openKbSettings = useCallback(() => {
    navigate(`/domain-knowledge/${id}/detail`, {
      state: { backTo: `/domain-knowledge/${id}` },
    });
  }, [navigate, id]);

  // 顶部统计栏 + 页头三个标签：抽成可复用回调，DocumentLibrary 增删文档或点「刷新」后经
  // onRefreshStats 触发，否则两者只在 id 变化时加载一次，会与列表脱节。
  // 页头标签取自 detail（与统计接口同源），所以一次刷新要把 detail 一并重新拉——
  // detail 的首次加载也走这里，不再另起一个 effect，否则挂载时会重复请求同一接口。
  const refreshStats = useCallback(() => {
    if (!id) return;
    getDocumentStats(id)
      .then(setDocStats)
      .catch(() => setDocStats(null));
    getDomainKnowledgeDetail(id)
      .then((next) => {
        setDetail(next);
        setDetailError(null);
      })
      // 拉失败保留旧值：页头不该因为一次刷新失败就清空成空白
      .catch((err) => {
        // 但「一次都没拉到」时必须报错：此前静默吞掉 404，页面只剩一个永不结束的
        // 骨架屏（编译资产面板的 detail==null 分支），用户看到的是「一直加载」。
        setDetailError((prev) => prev ?? getErrorMessage(err, t('domainKnowledge.loadErrorDesc')));
      });
  }, [id, t]);

  useEffect(() => {
    refreshStats();
  }, [refreshStats]);

  useEffect(() => {
    if (!id || !detail) return;
    // 数据源不受「结构化数据支持」开关控制（见 DomainKnowledgeDetail/tabVisibility.ts），
    // 关闭态也要如实展示已接入的数据源，不再走「—」占位。
    listDataSources(id)
      .then((list) => setDataSources(list.map((ds) => dataSourceInstanceDisplayName(ds, t)).join(' · ') || '—'))
      .catch(() => setDataSources('—'));
  }, [id, t, detail]);

  // 这份 Wiki 计数上提到宿主页，是因为页头标签要用它——面板已改成经 prop 拿，不再自己拉。
  // assetsMountKey 参与依赖：切进「编译资产」tab 时会连它一起重取，编译产物变了计数跟着变。
  // kbType 未定时先不动（isLlmWikiType(undefined) 为 false，等于按 lightrag 清空）。
  useEffect(() => {
    if (!id || !isLlmWikiType(detail?.kbType)) {
      setWikiStats(null);
      return;
    }
    getWikiContents(id, '')
      .then((res) =>
        setWikiStats({
          summaries: res.summaries?.length ?? 0,
          concepts: res.concepts?.length ?? 0,
          entities: res.entities?.length ?? 0,
        }),
      )
      .catch(() => setWikiStats(null));
  }, [id, detail?.kbType, assetsMountKey]);

  // 互斥四桶口径（与统计栏、后端 documents_stats 同源）：处理中+已完成+编译失败+解析失败=总计
  // 失败类指标常驻显示（即使为 0）。
  const stats: {
    labelKey: string;
    value: string;
    icon: JSX.Element;
    suffixKey?: string;
  }[] = [
    {
      labelKey: 'docPhase.total',
      value: (docStats?.total ?? detail?.documentCount ?? 0).toLocaleString(),
      suffixKey: 'docPhase.docSuffix',
      icon: <FileTextOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      labelKey: 'docPhase.processing',
      value: docStats ? docStats.processing.toLocaleString() : '—',
      icon: <ClockCircleOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      labelKey: 'docPhase.completedStat',
      value: docStats ? docStats.completed.toLocaleString() : '—',
      icon: <CheckCircleOutlined style={{ color: '#22c55e' }} />,
    },
    {
      labelKey: 'docPhase.compileFailedStat',
      value: docStats ? docStats.compileFailed.toLocaleString() : '—',
      icon: <BuildOutlined style={{ color: '#f59e0b' }} />,
    },
    {
      labelKey: 'docPhase.parseFailedStat',
      value: docStats ? docStats.parseFailed.toLocaleString() : '—',
      icon: <CloseCircleOutlined style={{ color: '#ef4444' }} />,
    },
    {
      labelKey: 'common.quotaDocLabel',
      value: docQuota ? `${docQuota.used} / ${docQuota.limit}` : '—',
      icon: <DatabaseOutlined style={{ color: '#8b5cf6' }} />,
    },
  ];

  // 编译资产三个总数放页头用标签展示：它们属于 KB 身份，与「知识库设置」页的页头标签同构，
  // 切到任一 tab 都看得见，不必再进「编译资产」读统计卡（面板里那几张卡已随之撤掉）。
  // lightrag 三个数直接取 detail——页面本来就要拉它，不额外发请求；口径见 buildAssetTags。
  const assetTags = detail ? buildAssetTags(t, detail, wikiStats) : [];

  return (
    <>
      <PageHeader
        backTo="/domain-knowledge"
        title={
          // wrap：库名 + 三个标签在窄屏放不下时换行，而不是把页头撑出横向滚动
          <Space align="center" size={10} wrap>
            <FileTextOutlined style={{ fontSize: 24, color: '#3b82f6' }} />
            <span>{detail?.name}</span>
            {assetTags.map((tag) => (
              <AssetStatTag key={tag.label} label={tag.label} value={tag.value} icon={tag.icon} tone={tag.tone} />
            ))}
          </Space>
        }
      />

      <PageContent>
        {detail == null && detailError ? (
          // 详情一次都没拉到时整页报错：文档库 / 编译资产 / 追踪三个页签全部依赖它，
          // 继续渲染只会得到空列表和一个永不结束的骨架屏。
          <Result
            status="warning"
            title={t('domainKnowledge.loadErrorTitle')}
            subTitle={
              <>
                <div>{detailError}</div>
                {/* 后端 404 同时覆盖「不存在」与「无权访问」，措辞用条件句，不泄露存在性 */}
                <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>
                  {t('domainKnowledge.loadErrorHint')}
                </div>
              </>
            }
            extra={
              <Button icon={<ReloadOutlined />} onClick={refreshStats}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : (
        <Tabs
          className="kb-detail-tabs"
          activeKey={activeTab}
          onChange={handleTabChange}
          tabBarExtraContent={{
            right: (
              <Button icon={<SettingOutlined />} onClick={openKbSettings}>
                {t('domainKnowledge.knowledgeBaseSettings')}
              </Button>
            ),
          }}
          items={[
            {
              key: TAB_DOCUMENTS,
              label: t('domainKnowledge.documentList'),
              children: (
                <div className="kb-detail-tab-pane">
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginBottom: 24,
                      flexShrink: 0,
                    }}
                  >
                    <Space align="center" size={24}>
                      {stats.map((s) => (
                        <Space key={s.labelKey} align="center" size={8}>
                          {s.icon}
                          <span style={{ color: '#64748b', fontSize: 14 }}>{t(s.labelKey)}</span>
                          <span style={{ color: '#0b2b5c', fontSize: 18, fontWeight: 700 }}>{s.value}</span>
                          {s.suffixKey && <span style={{ color: '#64748b', fontSize: 14 }}>{t(s.suffixKey)}</span>}
                        </Space>
                      ))}
                    </Space>
                    <Space align="center" size={6} style={{ color: '#64748b', fontSize: 14 }}>
                      <CloudServerOutlined style={{ color: '#8b5cf6' }} />
                      <span>
                        {t('dataSource.title')}: {dataSources}
                      </span>
                    </Space>
                  </div>

                  <div style={{ flex: 1, minHeight: 0 }}>
                    <DocumentLibrary
                      kbId={id}
                      kbType={detail?.kbType}
                      quotaError={quotaError}
                      docReached={docReached}
                      onRefreshQuota={reloadQuota}
                      onRefreshStats={refreshStats}
                    />
                  </div>
                </div>
              ),
            },
            {
              key: TAB_ASSETS,
              label: t('domainKnowledge.compileAssets'),
              // 面板由 antd Tabs 惰性挂载：首次切过来才发那批统计请求，不拖慢文档库首屏
              children: (
                <div className="kb-assets-tab-pane">
                  {detail == null ? (
                    // [jonex] 详情未返回前不渲染引擎面板：kb_type 未知时 CompileAssetsPanel
                    // 默认按 lightrag 渲染 Ontology，jonex_wiki 库会闪一下「先 Ontology 后 Wiki」。
                    <Skeleton active paragraph={{ rows: 6 }} />
                  ) : (
                    <CompileAssetsPanel key={assetsMountKey} kbId={id} kbType={detail.kbType} wikiStats={wikiStats} />
                  )}
                </div>
              ),
            },
            {
              key: TAB_TRACKING,
              label: t('route.tracking'),
              // 同样惰性挂载：反馈统计与明细只在首次切过来时才请求
              children: (
                <div className="kb-tracking-tab-pane">
                  <KnowledgeTracking embedded />
                </div>
              ),
            },
          ]}
        />
        )}
      </PageContent>
    </>
  );
}
