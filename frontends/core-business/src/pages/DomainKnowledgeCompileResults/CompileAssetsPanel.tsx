import { useState, useMemo, useEffect, useCallback } from 'react';
import type { CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Result, Tabs, Space, Skeleton } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { getErrorMessage } from '@jonex/shared-lib';
import type {
  OntologyStatistics,
  OntologyInstanceSummary,
  RelationInstanceSummary,
  KnowledgeBaseType,
  WikiStats,
} from '@/types/domainKnowledge';
import { getOntologyStatistics, getOntologyEntityTypes, getOntologyRelationTypes } from '@/api/domainKnowledge';
import { getSearchFeedbackStats } from '@/api/knowledgeSearch';
import type { SearchFeedbackStats } from '@/types/knowledgeSearch';
import { buildTabs, buildOpenkbTabs } from './config';
import type { TabConfig } from './config';
import OntologyTab from './OntologyTab';
import RelationTab from './RelationTab';
import GraphTab from './GraphTab';
import ColumnBrowseTab from './ColumnBrowseTab';
import WikiPageBrowser from './WikiPageBrowser';
import WikiGraphTab from './WikiGraphTab';
import WikiReaderShell from './wiki/WikiReaderShell';
import WikiColumnBoard from './wiki/WikiColumnBoard';
import LikeTab from './LikeTab';
import DislikeTab from './DislikeTab';

const renderTabLabel = (item: TabConfig) => (
  <Space size={6}>
    {item.icon}
    <span>{item.label}</span>
    {item.count > 0 && <span style={{ color: '#94a3b8', fontSize: 12 }}>{item.count}</span>}
  </Space>
);

// 骨架占位数量要对齐真实内容，否则加载完成时会跳变：
// 子 tab lightrag 4 个、openkb 2 个，占位取 4 覆盖两种
const TAB_SKELETON_KEYS = ['t1', 't2', 't3', 't4'];

// openkb 的「撑满 + 内层自滚」布局链：逐级把容器高度传到 Wiki 子组件，
// 让它们用 height:100% 对齐真实可用高度，而不是照视口估算
const PANEL_FILL_STYLE: CSSProperties = { height: '100%', display: 'flex', flexDirection: 'column' };
const CARD_BODY_FILL_STYLE: CSSProperties = {
  padding: 0,
  flex: 1,
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

interface CompileAssetsPanelProps {
  kbId: string;
  /**
   * [jonex] 知识库类型。由宿主页面传入——详情页已经拉过同一份 KB 详情，这里不再重复请求。
   * 异步就绪前为 undefined，面板先按 lightrag 渲染，就绪后由下方 effect 纠正子 tab。
   */
  kbType?: KnowledgeBaseType;
  /**
   * [jonex] llm-wiki 的全库页面计数（getWikiContents(kbId, "") 的三类页数）。
   * 由宿主页拉取并下发——页头标签与这里的 tab 计数要用同一份数据，各自拉会拉到两个时刻的快照。
   * null/undefined = 还没到位，按 0 渲染。
   */
  wikiStats?: WikiStats | null;
}

// 计数未到位时的占位：只在 tab 计数上体现，不影响内容渲染
const EMPTY_WIKI_STATS: WikiStats = { summaries: 0, concepts: 0, entities: 0 };

/**
 * 全库编译资产面板：纯内容面板，子 Tab 按库型分流（lightrag 本体/关系/图谱/列浏览；
 * llm-wiki 下为 Wiki 页面/图谱/列视图），不含页头——三处统计总数统一由宿主页面
 * （知识库详情页）的 PageHeader 用标签展示，这里不再重复一份。
 */
export default function CompileAssetsPanel({ kbId, kbType = 'lightrag', wikiStats = null }: CompileAssetsPanelProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState('ontology');
  const [statsData, setStatsData] = useState<OntologyStatistics | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  // 决定性请求（statistics）失败：lightrag 的 tab 全靠它的计数构建，必须整页报错而不是留空
  const [statsError, setStatsError] = useState<string | null>(null);
  // 次要请求（实体类型 / 关系类型）失败：tab 仍可渲染，只降级类型标签与建实例下拉，用横幅提示
  const [typesError, setTypesError] = useState<string | null>(null);
  const [entityTypes, setEntityTypes] = useState<OntologyInstanceSummary[] | null>(null);
  const [relationTypes, setRelationTypes] = useState<RelationInstanceSummary[] | null>(null);
  const [feedbackStats, setFeedbackStats] = useState<SearchFeedbackStats | null>(null);
  // 重试令牌：自增即重跑下面那批请求（错误态重试按钮与次要错误横幅共用）
  const [reloadToken, setReloadToken] = useState(0);
  // [jonex] openkb 分流：kbType 决定展示 Wiki 页面/图谱（本体数据为空）；
  // wikiCounts = 宿主页下发的全库三类页数，tab 计数用它
  const wikiCounts = wikiStats ?? EMPTY_WIKI_STATS;
  const isOpenKB = kbType === 'openkb';
  // [jonex] jonex_wiki 与 openkb 同属 LLM-Wiki，但前者走新的阅读/图谱/列式三视图
  // （与文档级 CompileResultPanel 一致），后者保持原页面树 + 实体关系图。
  const isJonexWiki = kbType === 'jonex_wiki';
  const isLlmWiki = isOpenKB || isJonexWiki;

  // [jonex] kbType 异步加载：挂载瞬间默认 lightrag（activeTab='ontology' 对 llm-wiki 无效），
  // kbType 返回 llm-wiki 后把 activeTab 对齐到有效 key（openkb: wikiPages/wikiGraph；
  // jonex_wiki: wikiPages/wikiColumns），覆盖首次默认 'ontology' 与用户误点
  // relation 的场景——首次进入即显示 Wiki 页面树/阅读界面
  useEffect(() => {
    const validKeys = isJonexWiki ? ['wikiPages', 'wikiColumns'] : ['wikiPages', 'wikiGraph'];
    if (isLlmWiki && !validKeys.includes(activeTab)) {
      setActiveTab('wikiPages');
    }
  }, [isLlmWiki, isJonexWiki, activeTab]);

  const retry = useCallback(() => setReloadToken((n) => n + 1), []);

  // 进入面板请求统计 + 本体实例 + 关系实例 + 反馈统计
  // 三个失败分支分档处理（不再静默吞错）：
  //   - statistics：决定性，失败 ⇒ statsError ⇒ lightrag 整页错误态（见下方渲染分支）；
  //   - entity/relation-types：次要，失败 ⇒ typesError ⇒ 顶部横幅提示，tab 与表格照常；
  //   - feedback-stats：只喂「用户赞/踩采纳」两个 tab 计数（当前未启用），失败降级为 0，
  //     不打扰用户——但仍显式落一个零值兜底，而不是把异常丢掉。
  useEffect(() => {
    if (!kbId) return;
    let alive = true;
    setStatsLoading(true);
    setStatsError(null);
    setTypesError(null);

    getOntologyStatistics(kbId)
      .then((data) => {
        if (alive) setStatsData(data);
      })
      .catch((err) => {
        if (alive) setStatsError(getErrorMessage(err, t('compile.assets.loadErrorDesc')));
      })
      .finally(() => {
        if (alive) setStatsLoading(false);
      });

    getOntologyEntityTypes(kbId)
      .then((res) => {
        if (alive) setEntityTypes(res.items.map((item) => ({ ...item, type: item.name })));
      })
      .catch((err) => {
        if (alive) setTypesError(getErrorMessage(err, t('compile.assets.partialError')));
      });

    getOntologyRelationTypes(kbId)
      .then((res) => {
        if (alive) setRelationTypes(res.items);
      })
      .catch((err) => {
        // 两个次要请求只留第一条错误文案，避免横幅内容来回跳
        if (alive) setTypesError((prev) => prev ?? getErrorMessage(err, t('compile.assets.partialError')));
      });

    getSearchFeedbackStats(kbId)
      .then((stats) => {
        if (alive) setFeedbackStats(stats);
      })
      .catch(() => {
        if (alive) setFeedbackStats({ total: 0, like_count: 0, dislike_count: 0 });
      });

    return () => {
      alive = false;
    };
  }, [kbId, reloadToken, t]);

  const tabs = useMemo(() => {
    // llm-wiki 的 tab 计数来自宿主页下发的 wikiStats，不依赖本体统计——
    // 统计请求失败也不能把这两类库的页面清空（子视图自己会报自己的错）
    if (isLlmWiki) return buildOpenkbTabs(t, wikiCounts, isJonexWiki);
    if (!statsData) return [];
    return buildTabs(t, statsData, feedbackStats?.like_count, feedbackStats?.dislike_count);
  }, [isLlmWiki, isJonexWiki, statsData, feedbackStats, wikiCounts, t]);
  // 首次加载（tab 计数还没到位）走骨架屏，撑住布局。
  // lightrag 的 tab 计数取自 statsData，llm-wiki 取自宿主页下发的 wikiStats——
  // 后者比 statsData 晚到也不算问题：计数只是 tab 头上一行灰字，补上时不引起跳变。
  const initialLoading = statsLoading;
  // lightrag 的 tab 构建依赖本体统计，统计 404/失败 ⇒ 整页错误态（含重试）。
  // llm-wiki 的 tab 用 wikiStats，统计失败不影响它们，交给子视图各自报错。
  const loadError = !isLlmWiki && !!statsError;
  // 次要错误横幅：只在 tab 能正常渲染时出现，避免与整页错误叠加
  const showTypesWarning = !loadError && !isLlmWiki && !!typesError;
  // 一律走「撑满」布局：子视图（表格 / 图谱 / 页面树）都要跟着容器高度自适应，
  // 内容超出时各自内部滚动。原先只有 llm-wiki 撑满、lightrag 依赖外层滚动，
  // 于是同一批子视图在文档级和知识库级两处呈现不一致——现在两边统一走这条链。

  const tabContent = useMemo(() => {
    // [jonex] jonex_wiki：与文档级 CompileResultPanel 同一套视图，全库模式 documentId 空
    if (isJonexWiki) {
      switch (activeTab) {
        case 'wikiPages':
          return <WikiReaderShell kbId={kbId} documentId="" />;
        case 'wikiColumns':
          return <WikiColumnBoard kbId={kbId} documentId="" height="100%" />;
        default:
          return null;
      }
    }
    if (isOpenKB) {
      switch (activeTab) {
        case 'wikiPages':
          // 全库模式：docId 空 = KB 级；不传 compileStatus（无单一文档编译状态）
          return <WikiPageBrowser kbId={kbId} docId="" fillHeight />;
        case 'wikiGraph':
          return <WikiGraphTab kbId={kbId} docId="" fillHeight />;
        default:
          return null;
      }
    }
    switch (activeTab) {
      case 'ontology':
        return <OntologyTab kbId={kbId} data={entityTypes} />;
      case 'relation':
        return <RelationTab kbId={kbId} data={relationTypes} />;
      case 'graph':
        // bare：卡面已由宿主页的 PageContent 承担，图谱不再自带一层壳，否则是「卡中卡」
        return <GraphTab kbId={kbId} bare />;
      case 'columnBrowse':
        // height 100%：与文档级一致，跟着容器走而不是照视口算 calc(100vh - N)
        return <ColumnBrowseTab kbId={kbId} height="100%" />;
      case 'like':
        return <LikeTab kbId={kbId} />;
      case 'dislike':
        return <DislikeTab kbId={kbId} />;
      default:
        return null;
    }
  }, [activeTab, kbId, entityTypes, relationTypes, isOpenKB, isJonexWiki]);

  return (
    <div style={PANEL_FILL_STYLE}>
      {/* Tabs & Table —— 不再套 Card：与同页「文档列表」tab 的落法一致（内容直接落在页面上，
          子 tab 与子组件各自带内边距），少一层白底容器嵌套 */}
      <div style={CARD_BODY_FILL_STYLE}>
        {initialLoading ? (
          // 骨架：一排 tab 头占位 + 内容区，与加载完成后的「Tabs」结构对齐
          // （Tabs 已不再加左右内边距，这里同步去掉，否则加载完成时内容会横跳 20px）
          <div style={{ paddingBottom: 20 }}>
            <Space size={32} style={{ height: 62, alignItems: 'center' }}>
              {TAB_SKELETON_KEYS.map((key) => (
                <Skeleton.Button key={key} active size="small" style={{ width: 78 }} />
              ))}
            </Space>
            <Skeleton active title={false} paragraph={{ rows: 5, width: '100%' }} />
          </div>
        ) : loadError ? (
          // 统一错误态：此前四个请求全部静默吞错，statsData 永远为空 ⇒ tabs=[] ⇒
          // 页面只剩一片空白（普通用户无密级时四个接口全 404 的真实表现）。
          <Result
            status="warning"
            title={t('compile.assets.loadErrorTitle')}
            subTitle={
              <>
                <div>{statsError}</div>
                {/* 与知识库页整页错误态同一措辞：404 同时覆盖「不存在」与「无权访问」 */}
                <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>
                  {t('domainKnowledge.loadErrorHint')}
                </div>
              </>
            }
            extra={
              <Button icon={<ReloadOutlined />} onClick={retry}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : (
          <>
            {showTypesWarning && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message={t('compile.assets.partialError')}
                action={
                  <Button size="small" onClick={retry}>
                    {t('common.retry')}
                  </Button>
                }
              />
            )}
            {/* className 供宿主样式去掉 .ant-tabs-nav 的默认下边距；
                不再自带左右内边距——内容直接落在宿主卡片的内边距上，少一层缩进 */}
            <Tabs
              className="kb-compile-assets-tabs"
              activeKey={activeTab}
              onChange={setActiveTab}
              items={tabs.map((t) => ({
                key: t.key,
                label: renderTabLabel(t),
              }))}
              style={{ flexShrink: 0, ...(isJonexWiki ? { position: 'sticky', top: 0, zIndex: 2, background: '#fff' } as CSSProperties : {}) }}
            />
            {/* 内容容器要是 flex column：子视图靠 flex:1 / height:100% 取高度，
                这里不给确定高度和方向，它们就没有可对齐的父级 */}
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{tabContent}</div>
          </>
        )}
      </div>
    </div>
  );
}
