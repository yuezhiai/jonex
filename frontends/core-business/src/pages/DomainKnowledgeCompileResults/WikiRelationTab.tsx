// [jonex] OpenKB Wiki 关系只读表 — 参照 RelationTab 去掉所有增删改入口
import { useState, useMemo, useEffect } from 'react';
import debounce from 'lodash/debounce';
import { Table, message, Input, Tooltip } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { WikiRelationshipRow } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import { getWikiRelationships } from '@/api/domainKnowledge';

const PAGE_SIZE = 10;

interface WikiRelationTabProps {
  kbId: string;
  title?: string;
}

export default function WikiRelationTab({ kbId, title: propTitle }: WikiRelationTabProps) {
  const { t } = useTranslation();
  const title = propTitle ?? t('compile.wikiRelations');

  const [rows, setRows] = useState<WikiRelationshipRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');

  const fetchData = useMemo(
    () =>
      debounce(async (p: number, kw: string, kId: string) => {
        setLoading(true);
        try {
          const res = await getWikiRelationships({
            kbId: kId,
            keyword: kw || undefined,
            page: p,
            pageSize: PAGE_SIZE,
          });
          setRows(res.list);
          setTotal(res.pagination.total);
        } catch {
          message.error(t('common.loadFailed'));
        } finally {
          setLoading(false);
        }
      }, 300),
    [],
  );

  useEffect(() => {
    setPage(1);
    fetchData(1, keyword.trim(), kbId);
  }, [keyword, kbId, fetchData]);

  const columns: ColumnsType<WikiRelationshipRow> = [
    {
      title: t('compile.relation.sourceObject'),
      dataIndex: 'source',
      key: 'source',
      width: 220,
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <span
            style={{
              display: 'inline-block',
              maxWidth: '100%',
              padding: '2px 8px',
              borderRadius: 6,
              background: '#eff6ff',
              color: '#3b82f6',
              fontSize: 12,
              fontWeight: 500,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {v || '—'}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t('compile.relation.targetObject'),
      dataIndex: 'target',
      key: 'target',
      width: 220,
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <span
            style={{
              display: 'inline-block',
              maxWidth: '100%',
              padding: '2px 8px',
              borderRadius: 6,
              background: '#ecfdf5',
              color: '#059669',
              fontSize: 12,
              fontWeight: 500,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {v || '—'}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t('common.description'),
      dataIndex: 'description',
      key: 'description',
      width: 320,
      ellipsis: true,
      render: (v: string) => <span style={{ color: '#64748b', fontSize: 13 }}>{v || '—'}</span>,
    },
  ];

  return (
    <>
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
            {t('compile.totalInstances', { count: total })}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Input
            prefix={<SearchOutlined />}
            placeholder={t('common.search')}
            style={{ width: 200 }}
            size="small"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            allowClear
          />
        </div>
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          onChange: (p) => {
            setPage(p);
            fetchData(p, keyword, kbId);
          },
          showSizeChanger: false,
          showTotal: (tTotal) => t('compile.totalInstances', { count: tTotal }),
        }}
        size="middle"
        style={{ padding: '0 24px 24px' }}
        scroll={{ y: 'calc(100vh - 480px)' }}
        locale={{
          emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyWikiRelations')}</span>,
        }}
      />
    </>
  );
}
