import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Empty, Modal, Select, Spin, Table, message } from 'antd';
import {
  ArrowLeftOutlined,
  LikeOutlined,
  DislikeOutlined,
  CheckCircleOutlined,
  CheckCircleFilled,
  EyeOutlined,
} from '@ant-design/icons';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import {
  getAnswerFeedbackList,
  getAnswerFeedbackStats,
  toggleAnswerFeedbackAdopted,
  deleteAnswerFeedback,
} from '@/api/knowledgeSearch';
import type { AnswerFeedbackListItem, AnswerFeedbackListResponse, AnswerFeedbackStats } from '@/types/knowledgeSearch';
import type { AnswerFeedbackSource } from '@/api/knowledgeSearch';
import FeedbackViewModal from './FeedbackViewModal';
import { getFeedbackSource, getMcpKeyLabel, getQuerySnapshot } from './feedbackSource';

type TabKey = 'all' | 'like' | 'dislike';
type SourceFilter = 'all' | AnswerFeedbackSource;

const TAB_CONFIG: { key: TabKey; labelKey: string; icon: React.ReactNode; color: string }[] = [
  { key: 'all', labelKey: 'domainManagementSearch.chip.all', icon: null, color: '#3b82f6' },
  { key: 'like', labelKey: 'knowledgeSearch.helpful', icon: <LikeOutlined />, color: '#059669' },
  { key: 'dislike', labelKey: 'knowledgeSearch.unhelpful', icon: <DislikeOutlined />, color: '#dc2626' },
];

interface KnowledgeTrackingProps {
  /**
   * [jonex] 嵌在知识库详情页的「情况追踪」tab 里时为 true：隐藏自带的返回按钮与标题。
   * 那一层外层已有页头与 tab，自带一套只会重复。独立页访问时保持原样。
   */
  embedded?: boolean;
}

/** 问答反馈追踪：统计卡 + 反馈筛选标签 + 明细表。独立页与详情页 tab 两用 */
export default function KnowledgeTracking({ embedded = false }: KnowledgeTrackingProps) {
  const { t } = useTranslation();
  const { id: knowledgeBaseId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState<TabKey>('all');
  // 来源筛选只影响列表与总数；统计卡仍是知识库全量（stats 接口无 source 参数）
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');

  // 数据
  const [feedbackData, setFeedbackData] = useState<AnswerFeedbackListResponse | null>(null);
  const [stats, setStats] = useState<AnswerFeedbackStats | null>(null);

  const [viewItem, setViewItem] = useState<AnswerFeedbackListItem | null>(null);

  // 知识库名称（从 URL 参数或从第一条记录获取）
  const kbName = searchParams.get('name') || t('tracking.knowledgeBase');

  const loadData = useCallback(async () => {
    if (!knowledgeBaseId) return;
    setLoading(true);
    setError('');
    try {
      const feedbackType = activeTab === 'all' ? undefined : activeTab;
      const source = sourceFilter === 'all' ? undefined : sourceFilter;
      const [listResult, statsResult] = await Promise.all([
        // 固定第 1 页 50 条、无分页；筛选变化时重新请求第一页
        getAnswerFeedbackList(knowledgeBaseId, { feedbackType, source, page: 1, pageSize: 50 }),
        getAnswerFeedbackStats(knowledgeBaseId),
      ]);
      setFeedbackData(listResult);
      setStats(statsResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [knowledgeBaseId, activeTab, sourceFilter]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleToggleAdopt = useCallback(async (item: AnswerFeedbackListItem) => {
    try {
      await toggleAnswerFeedbackAdopted(item.id);
      // 更新本地数据
      setFeedbackData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          items: prev.items.map((i) => (i.id === item.id ? { ...i, adopted: !i.adopted } : i)),
        };
      });
      message.success(t(item.adopted ? 'compile.cancelAdopted' : 'compile.adopted'));
    } catch (err: any) {
      message.error(err?.message || t('common.operationFailed'));
    }
  }, []);

  const handleView = useCallback((item: AnswerFeedbackListItem) => {
    setViewItem(item);
  }, []);

  const handleViewClose = useCallback(() => {
    setViewItem(null);
  }, []);

  /** 删除单条反馈记录 */
  const handleDeleteFeedback = useCallback(async (item: AnswerFeedbackListItem) => {
    Modal.confirm({
      title: t('compile.feedback.deleteFeedback'),
      content: t('compile.confirmDeleteFeedback', {
        query: getQuerySnapshot(item) ?? t('tracking.mcpNoQuerySnapshot'),
      }),
      okText: t('common.okText'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await deleteAnswerFeedback(item.id);
          // 更新本地数据：移除该项
          setFeedbackData((prev) => {
            if (!prev) return prev;
            const newItems = prev.items.filter((i) => i.id !== item.id);
            // 同步更新统计
            const likeCount = item.feedback_type === 'like' ? prev.like_count - 1 : prev.like_count;
            const dislikeCount = item.feedback_type === 'dislike' ? prev.dislike_count - 1 : prev.dislike_count;
            return {
              ...prev,
              items: newItems,
              total: prev.total - 1,
              like_count: Math.max(0, likeCount),
              dislike_count: Math.max(0, dislikeCount),
            };
          });
          message.success(t('tracking.feedbackDeleted'));
        } catch (err: any) {
          message.error(err?.message || t('common.deleteFailed'));
        }
      },
    });
  }, []);

  // ── 渲染统计卡片 ──

  const renderStats = () => {
    const cards = [
      {
        labelKey: 'tracking.totalFeedback',
        value: stats?.total ?? '-',
        icon: null,
        bg: '#eff6ff',
        color: '#3b82f6',
      },
      {
        labelKey: 'knowledgeSearch.helpful',
        value: stats?.like_count ?? '-',
        icon: <LikeOutlined />,
        bg: '#ecfdf5',
        color: '#059669',
      },
      {
        labelKey: 'knowledgeSearch.unhelpful',
        value: stats?.dislike_count ?? '-',
        icon: <DislikeOutlined />,
        bg: '#fef2f2',
        color: '#dc2626',
      },
      {
        labelKey: 'tracking.satisfactionRate',
        value: stats && stats.total > 0 ? `${((stats.like_count / stats.total) * 100).toFixed(1)}%` : '-',
        icon: null,
        bg: '#f5f3ff',
        color: '#7c3aed',
      },
    ];

    return (
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 24 }}>
        {cards.map((c) => (
          <Card
            key={c.labelKey}
            style={{ borderRadius: 12 }}
            styles={{ body: { padding: '18px 20px', display: 'flex', alignItems: 'center', gap: 14 } }}
          >
            {c.icon && (
              <div
                style={{
                  width: 42,
                  height: 42,
                  borderRadius: 10,
                  background: c.bg,
                  color: c.color,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 18,
                  flexShrink: 0,
                }}
              >
                {c.icon}
              </div>
            )}
            <div>
              <div
                style={{
                  fontSize: 24,
                  fontWeight: 700,
                  color: '#0b2b5c',
                  lineHeight: 1.2,
                }}
              >
                {loading ? '-' : c.value}
              </div>
              <div style={{ fontSize: 13, color: '#94a3b8', marginTop: 2 }}>{t(c.labelKey)}</div>
            </div>
          </Card>
        ))}
      </div>
    );
  };

  // ── 标签切换 ──

  const renderTabs = () => (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 12,
        borderBottom: '2px solid #eef2f6',
        marginBottom: 20,
      }}
    >
      <div style={{ display: 'flex', gap: 0 }}>
        {TAB_CONFIG.map((tab) => {
          const isActive = activeTab === tab.key;
          return (
            <Button
              key={tab.key}
              type="text"
              onClick={() => setActiveTab(tab.key)}
              style={{
                padding: '10px 24px',
                fontSize: 14,
                fontWeight: 500,
                color: isActive ? tab.color : '#94a3b8',
                borderBottom: `2px solid ${isActive ? tab.color : 'transparent'}`,
                marginBottom: -2,
                borderRadius: 0,
                transition: 'all 0.2s',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                height: 'auto',
                lineHeight: 'inherit',
              }}
            >
              {tab.icon}
              {t(tab.labelKey)}
            </Button>
          );
        })}
      </div>
      <Select<SourceFilter>
        value={sourceFilter}
        onChange={setSourceFilter}
        aria-label={t('tracking.columnSource')}
        style={{ width: 150, marginBottom: 8 }}
        options={[
          { value: 'all', label: t('tracking.sourceAll') },
          { value: 'web', label: t('tracking.sourceWeb') },
          { value: 'mcp', label: t('tracking.sourceMcp') },
        ]}
      />
    </div>
  );

  // ── 格式化时间 ──

  const formatTime = (iso: string | null) => {
    if (!iso) return '-';
    try {
      const d = new Date(iso);
      const pad = (n: number) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch {
      return iso;
    }
  };

  const truncateText = (text: string | null, max = 80) => {
    if (!text) return '-';
    const cleaned = text.replace(/\s+/g, ' ').trim();
    return cleaned.length > max ? cleaned.slice(0, max) + '...' : cleaned;
  };

  // ── 表格 ──

  const renderTable = () => {
    const items = feedbackData?.items ?? [];

    if (items.length === 0) {
      return (
        <div style={{ padding: '60px 0' }}>
          <Empty description={t('tracking.noFeedback')} />
        </div>
      );
    }

    const columns = [
      {
        title: t('tracking.columnTime'),
        dataIndex: 'created_at',
        key: 'time',
        width: 150,
        render: (val: string | null) => (
          <span style={{ whiteSpace: 'nowrap', color: '#94a3b8', fontSize: 12 }}>{formatTime(val)}</span>
        ),
      },
      {
        title: t('tracking.columnSource'),
        dataIndex: 'mcp_key_id',
        key: 'source',
        width: 150,
        render: (_: string | null, record: AnswerFeedbackListItem) => {
          const isMcp = getFeedbackSource(record) === 'mcp';
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  alignSelf: 'flex-start',
                  padding: '2px 10px',
                  borderRadius: 6,
                  fontSize: 12,
                  fontWeight: 500,
                  background: isMcp ? '#f5f3ff' : '#eff6ff',
                  color: isMcp ? '#7c3aed' : '#3b82f6',
                }}
              >
                {t(isMcp ? 'tracking.sourceMcp' : 'tracking.sourceWeb')}
              </span>
              {isMcp ? (
                <span
                  style={{
                    fontSize: 11,
                    color: '#94a3b8',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={getMcpKeyLabel(record)}
                >
                  {getMcpKeyLabel(record)}
                </span>
              ) : null}
            </div>
          );
        },
      },
      {
        title: t('compile.feedback.question'),
        dataIndex: 'query',
        key: 'query',
        width: 240,
        render: (_: string | null, record: AnswerFeedbackListItem) => {
          const query = getQuerySnapshot(record);
          if (!query) {
            return (
              <span style={{ color: '#94a3b8', fontSize: 12 }}>
                {record.mcp_key_id ? t('tracking.mcpNoQuerySnapshot') : '-'}
              </span>
            );
          }
          return (
            <div
              style={{
                fontWeight: 500,
                color: '#1e293b',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={query}
            >
              {query}
            </div>
          );
        },
      },
      {
        title: t('tracking.columnAnswer'),
        dataIndex: 'answer',
        key: 'answer',
        width: 300,
        render: (val: string | null) => (
          <div
            style={{
              color: '#64748b',
              fontSize: 12,
              lineHeight: 1.6,
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
          >
            {truncateText(val, 120)}
          </div>
        ),
      },
      {
        title: t('common.type'),
        dataIndex: 'feedback_type',
        key: 'type',
        width: 150,
        render: (val: string, record: AnswerFeedbackListItem) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
                padding: '2px 10px',
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 500,
                background: val === 'like' ? '#ecfdf5' : '#fef2f2',
                color: val === 'like' ? '#059669' : '#dc2626',
                alignSelf: 'flex-start',
              }}
            >
              {val === 'like' ? <LikeOutlined /> : <DislikeOutlined />}
              {val === 'like' ? t('knowledgeSearch.helpful') : t('knowledgeSearch.unhelpful')}
            </span>
            {val === 'dislike' && record.feedback_reason ? (
              <span style={{ fontSize: 11, color: '#94a3b8' }}>
                {t(`knowledgeSearch.feedbackReason_${record.feedback_reason}`)}
              </span>
            ) : null}
          </div>
        ),
      },
      {
        title: t('tracking.columnStatus'),
        dataIndex: 'adopted',
        key: 'status',
        width: 110,
        render: (adopted: boolean) => (
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              fontSize: 12,
              color: adopted ? '#059669' : '#94a3b8',
            }}
          >
            {adopted ? <CheckCircleFilled style={{ color: '#059669' }} /> : <CheckCircleOutlined />}
            {adopted ? t('compile.adopted') : t('tracking.notAdopted')}
          </span>
        ),
      },
      {
        title: t('common.actions'),
        key: 'actions',
        width: 210,
        render: (_: unknown, record: AnswerFeedbackListItem) => (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            <Button
              onClick={() => handleView(record)}
              style={{
                padding: '3px 12px',
                fontSize: 12,
                color: '#64748b',
                background: '#f1f5f9',
                border: '1px solid #e2e8f0',
                borderRadius: 6,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              <EyeOutlined /> {t('permission.view')}
            </Button>
            <Button
              onClick={() => handleToggleAdopt(record)}
              style={{
                padding: '3px 12px',
                fontSize: 12,
                color: record.adopted ? '#059669' : '#3b82f6',
                background: record.adopted ? '#ecfdf5' : '#eff6ff',
                border: `1px solid ${record.adopted ? '#a7f3d0' : '#bfdbfe'}`,
                borderRadius: 6,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              {record.adopted ? t('compile.adopted') : t('compile.feedback.adopt')}
            </Button>
            <Button
              onClick={() => handleDeleteFeedback(record)}
              style={{
                padding: '3px 12px',
                fontSize: 12,
                color: '#ef4444',
                background: '#fef2f2',
                border: '1px solid #fecaca',
                borderRadius: 6,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              {t('common.delete')}
            </Button>
          </div>
        ),
      },
    ];

    return (
      <Table<AnswerFeedbackListItem>
        columns={columns}
        dataSource={items}
        rowKey="id"
        pagination={false}
        size="middle"
        rowClassName={() => 'tracking-row'}
        scroll={{ x: 1310 }}
      />
    );
  };

  // 表格区（loading / error / 表格三态）：嵌 tab 与独立页两种外层容器共用同一份内容
  const tableBlock = loading ? (
    <div style={{ textAlign: 'center', padding: '60px 0' }}>
      <Spin size="large" />
    </div>
  ) : error ? (
    <div style={{ textAlign: 'center', padding: '60px 0' }}>
      <div style={{ color: '#ef4444', fontSize: 14, marginBottom: 12 }}>{error}</div>
      <Button onClick={() => void loadData()}>{t('tracking.reload')}</Button>
    </div>
  ) : (
    renderTable()
  );

  // ── 主渲染 ──

  return (
    <div>
      {/* 头部：嵌入 tab 时整块不要（外层页头 + tab 已表达同一信息） */}
      {!embedded && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            marginBottom: 24,
          }}
        >
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(-1)}
            style={{
              fontSize: 14,
              color: '#64748b',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              borderRadius: 8,
            }}
          >
            {t('common.back')}
          </Button>
          <h1
            style={{
              fontSize: 22,
              fontWeight: 700,
              color: '#0b2b5c',
              display: 'flex',
              alignItems: 'center',
              gap: 10,
            }}
          >
            {t('route.tracking')}
            <span style={{ fontSize: 14, fontWeight: 400, color: '#94a3b8' }}>— {kbName}</span>
          </h1>
        </div>
      )}

      {/* 统计卡片 */}
      {renderStats()}

      {/* 标签 + 表格 —— 嵌在详情页 tab 里时不套外层 Card：与同页「文档列表」tab 的落法一致
          （内容直接落在页面上）；独立页没有外层页面容器承接，保留卡片 */}
      {embedded ? (
        <div>
          {renderTabs()}
          {tableBlock}
        </div>
      ) : (
        <Card style={{ borderRadius: 12 }} styles={{ body: { padding: '20px 24px' } }}>
          {renderTabs()}
          {tableBlock}
        </Card>
      )}

      <FeedbackViewModal open={viewItem !== null} item={viewItem} onClose={handleViewClose} />
    </div>
  );
}
