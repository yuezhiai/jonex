// [jonex] OpenKB Wiki 实体/概念只读表 — 参照 OntologyTab 去掉所有增删改入口
import { useState, useMemo, useEffect } from 'react';
import debounce from 'lodash/debounce';
import { Table, message, Input } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { WikiEntityRow } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import { getWikiEntities } from '@/api/domainKnowledge';

const PAGE_SIZE = 10;

interface WikiEntityTabProps {
  kbId: string;
  title?: string;
}

export default function WikiEntityTab({ kbId, title: propTitle }: WikiEntityTabProps) {
  const { t } = useTranslation();
  const title = propTitle ?? t('compile.wikiEntities');

  const [rows, setRows] = useState<WikiEntityRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');

  const fetchData = useMemo(
    () =>
      debounce(async (p: number, kw: string, kId: string) => {
        setLoading(true);
        try {
          const res = await getWikiEntities({
            kbId: kId,
            keyword: kw || undefined,
            page: p,
            pageSize: PAGE_SIZE,
          });
          setRows(res.list);
          setTotal(res.pagination.total);
        } catch (err: any) {
          message.error(err?.message || t('common.loadFailed'));
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

  const columns: ColumnsType<WikiEntityRow> = [
    {
      title: t('domainKnowledge.entityType'),
      dataIndex: 'type',
      key: 'type',
      width: 140,
      render: (v: string) =>
        v ? (
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 6,
              background: '#eff6ff',
              color: '#3b82f6',
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            {v}
          </span>
        ) : (
          <span style={{ color: '#94a3b8' }}>—</span>
        ),
    },
    {
      title: t('compile.instanceName'),
      dataIndex: 'name',
      key: 'name',
      width: 260,
      render: (v: string) => <strong style={{ color: '#0b2b5c' }}>{v || '—'}</strong>,
    },
    {
      title: t('common.description'),
      dataIndex: 'description',
      key: 'description',
      width: 400,
      ellipsis: true,
      render: (v: string) => <span style={{ color: '#64748b', fontSize: 13 }}>{v || '—'}</span>,
    },
    {
      title: t('compile.relation.name'),
      dataIndex: 'relationsCount',
      key: 'relationsCount',
      width: 90,
      render: (v: number) => v ?? 0,
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
          emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyWikiEntities')}</span>,
        }}
      />
    </>
  );
}
