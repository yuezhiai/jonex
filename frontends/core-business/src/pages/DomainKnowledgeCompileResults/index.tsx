import { useState, useMemo, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import { Button, Card, Tag, Tabs, Space, Spin } from 'antd';
import { ArrowLeftOutlined, DatabaseOutlined } from '@ant-design/icons';
import type {
  OntologyStatistics,
  OntologyInstanceSummary,
  RelationInstanceSummary,
  KnowledgeBaseType,
} from '@/types/domainKnowledge';
import {
  getOntologyStatistics,
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  getDomainKnowledgeDetail,
  getWikiContents,
} from '@/api/domainKnowledge';
import { getSearchFeedbackStats } from '@/api/knowledgeSearch';
import type { SearchFeedbackStats } from '@/types/knowledgeSearch';
import { buildTabs, buildStats, buildOpenkbTabs, buildOpenkbStats } from './config';
import type { TabConfig, WikiStats } from './config';
import OntologyTab from './OntologyTab';
import RelationTab from './RelationTab';
import GraphTab from './GraphTab';
import WikiPageBrowser from './WikiPageBrowser';
import WikiGraphTab from './WikiGraphTab';
import LikeTab from './LikeTab';
import DislikeTab from './DislikeTab';

const renderTabLabel = (item: TabConfig) => (
  <Space size={6}>
    {item.icon}
    <span>{item.label}</span>
    {item.count > 0 && <span style={{ color: '#94a3b8', fontSize: 12 }}>{item.count}</span>}
  </Space>
);

export default function DomainKnowledgeCompileResults() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id = '' } = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState('ontology');
  const [statsData, setStatsData] = useState<OntologyStatistics | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);

  const [entityTypes, setEntityTypes] = useState<OntologyInstanceSummary[] | null>(null);
  const [relationTypes, setRelationTypes] = useState<RelationInstanceSummary[] | null>(null);
  const [feedbackStats, setFeedbackStats] = useState<SearchFeedbackStats | null>(null);
  // [jonex] openkb 分流：kbType 决定展示 Wiki 页面/图谱（本体数据为空）；
  // wikiStats = getWikiContents(kbId, "") 全库三类页数（stats 卡片 + tab count 同源）
  const [kbType, setKbType] = useState<KnowledgeBaseType>('lightrag');
  const [wikiStats, setWikiStats] = useState<WikiStats>({ summaries: 0, concepts: 0, entities: 0 });
  const isOpenKB = kbType === 'openkb';

  // [jonex] kbType 异步加载：挂载瞬间默认 lightrag（activeTab='ontology' 对 openkb 无效），
  // kbType 返回 openkb 后把 activeTab 对齐到 openkb 的有效 key（'wikiPages'/'wikiGraph'），
  // 覆盖首次默认 'ontology' 与用户误点 relation 的场景——首次进入即显示 Wiki 页面树
  useEffect(() => {
    if (isOpenKB && !['wikiPages', 'wikiGraph'].includes(activeTab)) {
      setActiveTab('wikiPages');
    }
  }, [isOpenKB, activeTab]);

  // 进入页面请求统计 + 本体实例 + 关系实例 + 反馈统计 + kbType
  useEffect(() => {
    if (!id) return;
    setStatsLoading(true);

    getDomainKnowledgeDetail(id)
      .then((detail) => setKbType(detail.kbType || 'lightrag'))
      .catch(() => setKbType('lightrag'));

    getOntologyStatistics(id)
      .then(setStatsData)
      .catch(() => {})
      .finally(() => setStatsLoading(false));

    getOntologyEntityTypes(id)
      .then((res) => setEntityTypes(res.items.map((item) => ({ ...item, type: item.name }))))
      .catch(() => {});

    getOntologyRelationTypes(id)
      .then((res) => setRelationTypes(res.items))
      .catch(() => {});

    getSearchFeedbackStats(id)
      .then(setFeedbackStats)
      .catch(() => {});
  }, [id]);

  // [jonex] openkb：加载全库 Wiki 内容统计（document_id 空 = KB 级）
  useEffect(() => {
    if (!id || kbType !== 'openkb') return;
    getWikiContents(id, '')
      .then((res) => {
        setWikiStats({
          summaries: res.summaries?.length ?? 0,
          concepts: res.concepts?.length ?? 0,
          entities: res.entities?.length ?? 0,
        });
      })
      .catch(() => {});
  }, [id, kbType]);

  const tabs = useMemo(() => {
    if (!statsData) return [];
    return isOpenKB
      ? buildOpenkbTabs(t, wikiStats)
      : buildTabs(t, statsData, feedbackStats?.like_count, feedbackStats?.dislike_count);
  }, [isOpenKB, statsData, feedbackStats, wikiStats, t]);
  const stats = useMemo(
    () =>
      statsData
        ? isOpenKB
          ? buildOpenkbStats(t, statsData.source_file_count, wikiStats)
          : buildStats(t, statsData)
        : [],
    [isOpenKB, statsData, wikiStats, t],
  );

  const tabContent = useMemo(() => {
    if (isOpenKB) {
      switch (activeTab) {
        case 'wikiPages':
          // 全库模式：docId 空 = KB 级；不传 compileStatus（无单一文档编译状态）
          return <WikiPageBrowser kbId={id} docId="" />;
        case 'wikiGraph':
          return <WikiGraphTab kbId={id} docId="" />;
        default:
          return null;
      }
    }
    switch (activeTab) {
      case 'ontology':
        return <OntologyTab kbId={id} data={entityTypes} />;
      case 'relation':
        return <RelationTab kbId={id} data={relationTypes} />;
      case 'graph':
        return <GraphTab kbId={id} />;
      case 'like':
        return <LikeTab kbId={id} />;
      case 'dislike':
        return <DislikeTab kbId={id} />;
      default:
        return null;
    }
  }, [activeTab, id, entityTypes, relationTypes, isOpenKB]);

  return (
    <div>
      {/* Header */}
      <div
        style={{
          background: '#fff',
          borderRadius: 12,
          padding: '20px 24px',
          border: '1px solid #eef2f6',
          marginBottom: 16,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            marginBottom: 12,
          }}
        >
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(`/domain-knowledge/${id}`)}
            style={{ borderRadius: 8 }}
          >
            {t('common.back')}
          </Button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flex: 1 }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: 10,
                background: '#eff6ff',
                color: '#3b82f6',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 20,
              }}
            >
              <DatabaseOutlined />
            </div>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontSize: 18, fontWeight: 700, color: '#0b2b5c' }}>
                  {statsData?.knowledge_base_name || t('domainKnowledge.detail')}
                </span>
                <Tag color="success" style={{ margin: 0, borderRadius: 6, fontSize: 12 }}>
                  {t('status.compiled')}
                </Tag>
              </div>
              <div
                style={{
                  fontSize: 13,
                  color: '#94a3b8',
                  marginTop: 4,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 16,
                }}
              >
                <span>{t('common.fullCompileResults')}</span>
                {statsData?.last_update_time && <span>{new Date(statsData.last_update_time).toLocaleString()}</span>}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Stats */}
      <Spin spinning={statsLoading}>
        <div
          style={{
            display: 'flex',
            gap: 16,
            marginBottom: 16,
          }}
        >
          {stats.map((s) => (
            <Card
              key={s.label}
              styles={{
                body: {
                  padding: '20px 24px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 16,
                },
              }}
              style={{
                flex: 1,
                borderRadius: 12,
                border: '1px solid #eef2f6',
                boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
              }}
            >
              <div
                style={{
                  width: 48,
                  height: 48,
                  borderRadius: 12,
                  background: '#f8fafc',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 22,
                }}
              >
                {s.icon}
              </div>
              <div>
                <div
                  style={{
                    fontSize: 24,
                    fontWeight: 700,
                    color: '#0b2b5c',
                    lineHeight: 1,
                  }}
                >
                  {s.value.toLocaleString()}
                </div>
                <div style={{ fontSize: 13, color: '#94a3b8', marginTop: 4 }}>{s.label}</div>
              </div>
            </Card>
          ))}
        </div>
      </Spin>

      {/* Tabs & Table */}
      <Card
        style={{
          borderRadius: 12,
          border: '1px solid #eef2f6',
          boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
          overflow: 'hidden',
        }}
        styles={{ body: { padding: 0 } }}
      >
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={tabs.map((t) => ({
            key: t.key,
            label: renderTabLabel(t),
          }))}
          style={{ padding: '0 20px' }}
        />
        <div>{tabContent}</div>
      </Card>
    </div>
  );
}
