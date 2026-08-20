import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { Table, Space, Modal, message } from 'antd';
import { EditOutlined, DeleteOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { getSearchFeedbackList, toggleSearchFeedbackAdopted, cancelSearchFeedback } from '@/api/knowledgeSearch';
import type { SearchFeedbackItem, SearchFeedbackType } from '@/types/knowledgeSearch';
import { useTranslation } from 'react-i18next';

interface FeedbackListProps {
  kbId: string;
  feedbackType: SearchFeedbackType;
  title: string;
}

const PAGE_SIZE = 10;

export default function FeedbackList({ kbId, feedbackType, title }: FeedbackListProps) {
  const { canWrite } = useKbPermission(kbId);
  const { t } = useTranslation();
  const [data, setData] = useState<SearchFeedbackItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  // 有关键词时缓存全量过滤结果，用于客户端分页
  const [cachedFiltered, setCachedFiltered] = useState<SearchFeedbackItem[]>([]);
  const keywordRef = useRef(keyword);
  keywordRef.current = keyword;

  // 有关键词时：拉取全量数据 → 客户端过滤 → 缓存 → 客户端分页
  const fullFetch = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    try {
      const kw = keywordRef.current.trim();
      if (kw) {
        const result = await getSearchFeedbackList(kbId, {
          feedbackType,
          page: 1,
          pageSize: 100,
        });
        const filtered = result.items.filter(
          (item) =>
            item.query.toLowerCase().includes(kw.toLowerCase()) ||
            (item.answer_preview || '').toLowerCase().includes(kw.toLowerCase()),
        );
        setCachedFiltered(filtered);
        setTotal(filtered.length);
        setData(filtered.slice(0, PAGE_SIZE));
      }
    } catch (err: any) {
      message.error(err?.message || t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, feedbackType]);

  // 无关键词时：正常服务端分页
  const pageFetch = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    try {
      const result = await getSearchFeedbackList(kbId, {
        feedbackType,
        page,
        pageSize: PAGE_SIZE,
      });
      setData(result.items);
      setTotal(result.total);
    } catch (err: any) {
      message.error(err?.message || t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, feedbackType, page]);

  // 关键词变化 → 全量拉取
  useEffect(() => {
    if (keyword.trim()) {
      fullFetch();
    }
  }, [fullFetch, keyword]);

  // 无关键词时 → 服务端分页
  useEffect(() => {
    if (!keyword.trim()) {
      pageFetch();
    }
  }, [pageFetch, keyword]);

  // 关键词模式下，翻页时从缓存切片
  useEffect(() => {
    if (keyword.trim()) {
      const start = (page - 1) * PAGE_SIZE;
      setData(cachedFiltered.slice(start, start + PAGE_SIZE));
    }
  }, [page, keyword, cachedFiltered]);

  const handleToggleAdopt = async (item: SearchFeedbackItem) => {
    try {
      await toggleSearchFeedbackAdopted(item.id);
      setData((prev) => prev.map((i) => (i.id === item.id ? { ...i, adopted: !i.adopted } : i)));
      message.success(item.adopted ? t('compile.cancelAdopted') : t('compile.adopted'));
    } catch (err: any) {
      message.error(err?.message || t('common.operationFailed'));
    }
  };

  const handleDelete = (item: SearchFeedbackItem) => {
    Modal.confirm({
      title: t('compile.feedback.deleteFeedback'),
      content: t('compile.confirmDeleteFeedback', { query: item.query }),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await cancelSearchFeedback({
            sessionId: item.session_id,
            feedbackType: item.feedback_type,
            kbIds: [item.knowledge_base_id],
          });
          setData((prev) => prev.filter((i) => i.id !== item.id));
          setTotal((prev) => Math.max(0, prev - 1));
          message.success(t('common.deleteSuccess'));
        } catch (err: any) {
          message.error(err?.message || t('common.deleteFailed'));
        }
      },
    });
  };

  const truncate = (text: string | null, max = 80) => {
    if (!text) return '—';
    const cleaned = text.replace(/\s+/g, ' ').trim();
    return cleaned.length > max ? cleaned.slice(0, max) + '...' : cleaned;
  };

  const columns: ColumnsType<SearchFeedbackItem> = useMemo(
    () => [
      {
        title: t('compile.feedback.question'),
        dataIndex: 'query',
        key: 'query',
        width: '35%',
        render: (v: string) => (
          <div
            style={{
              fontWeight: 500,
              color: '#0b2b5c',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={v}
          >
            {v}
          </div>
        ),
      },
      {
        title: t('compile.feedback.answer'),
        dataIndex: 'answer_preview',
        key: 'answer_preview',
        width: '45%',
        render: (v: string | null) => (
          <div
            style={{
              color: '#64748b',
              fontSize: 13,
              lineHeight: 1.6,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
            }}
          >
            {truncate(v, 120)}
          </div>
        ),
      },
      {
        title: t('common.actions'),
        key: 'action',
        width: 120,
        align: 'center',
        render: (_: unknown, record: SearchFeedbackItem) => (
          <Space size={12}>
            <a
              className="yx-table-action"
              style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
              title={record.adopted ? t('compile.feedback.cancelAdopt') : t('compile.feedback.adopt')}
              onClick={() => canWrite && handleToggleAdopt(record)}
            >
              <EditOutlined />
            </a>
            <a
              className="yx-table-action"
              style={canWrite ? { color: '#ef4444' } : { color: '#ef4444', opacity: 0.4, cursor: 'not-allowed' }}
              title={t('common.delete')}
              onClick={() => canWrite && handleDelete(record)}
            >
              <DeleteOutlined />
            </a>
          </Space>
        ),
      },
    ],
    [],
  );

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '16px 24px',
          borderTop: '1px solid #f1f5f9',
        }}
      >
        <div style={{ fontSize: 14, fontWeight: 600, color: '#0b2b5c' }}>
          {title}
          <span style={{ fontSize: 13, color: '#94a3b8', fontWeight: 400, marginLeft: 8 }}>
            {t('common.totalItems', { total })}
          </span>
        </div>
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={data}
        loading={loading}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          showTotal: (totalCount) => t('common.totalItems', { total: totalCount }),
          onChange: (p) => setPage(p),
        }}
        size="middle"
        style={{ padding: '0 24px 24px' }}
        locale={{ emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyFeedback')}</span> }}
      />
    </div>
  );
}
