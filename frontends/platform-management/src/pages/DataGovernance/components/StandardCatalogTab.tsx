import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Breadcrumb,
  Button,
  Empty,
  Input,
  Skeleton,
  Space,
  Table,
  Tabs,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import {
  getStandardDomain,
  listKbSchemas,
  listStandardDomains,
} from '@/api/standardCatalog';
import type {
  DgKbSchema,
  DgStandardDomain,
  DgStandardScenario,
} from '../standardTypes';
import {
  displayNameOrId,
  isUnpublished,
  kbEntryDomainAction,
  loadErrorText,
  pageAfterSizeChange,
  templateEntryAction,
} from '../standardUtils';
import { DG_PAGE_SIZE, DG_PAGE_SIZE_OPTIONS } from '../constant';
import { BindingStatusTag, DocStatsCell } from './StandardCatalogBits';
import KbSchemaDrawer from './KbSchemaDrawer';
import StandardScenarioDrawer from './StandardScenarioDrawer';

type CatalogTab = 'template' | 'kb';

interface DomainDetail {
  domain: DgStandardDomain;
  scenarios: DgStandardScenario[];
}

/**
 * 标准目录：模板标准（领域 → 场景 → 场景抽屉）与知识库应用（KB schema → 详情抽屉）。
 *
 * 只做展示与导航，不提供写操作；权限由页面 `canRead` 在渲染前控制。
 * 所有请求都带序号：切领域/切 Tab/关抽屉后，旧响应一律丢弃。
 */
export default function StandardCatalogTab(): JSX.Element {
  const { t } = useTranslation();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [activeTab, setActiveTab] = useState<CatalogTab>('template');

  // 模板标准
  const [domains, setDomains] = useState<DgStandardDomain[]>([]);
  const [domainsLoading, setDomainsLoading] = useState(false);
  const [domainsError, setDomainsError] = useState<string | null>(null);
  const [selectedDomainId, setSelectedDomainId] = useState<string | null>(null);
  const [domainDetail, setDomainDetail] = useState<DomainDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  // 场景抽屉自己的领域名：只由打开入口写入，不复用可能属于别的领域的 domainDetail。
  const [scenarioDomainName, setScenarioDomainName] = useState<string | null>(null);

  // 知识库应用
  const [kbItems, setKbItems] = useState<DgKbSchema[]>([]);
  const [kbTotal, setKbTotal] = useState(0);
  const [kbPage, setKbPage] = useState(1);
  const [kbPageSize, setKbPageSize] = useState(DG_PAGE_SIZE);
  const [kbKeyword, setKbKeyword] = useState('');
  const [kbAppliedKeyword, setKbAppliedKeyword] = useState('');
  const [kbLoading, setKbLoading] = useState(false);
  const [kbError, setKbError] = useState<string | null>(null);
  const [kbSelected, setKbSelected] = useState<DgKbSchema | null>(null);

  const domainsSeq = useRef(0);
  const detailSeq = useRef(0);
  const kbSeq = useRef(0);

  const loadDomains = useCallback(async () => {
    const my = ++domainsSeq.current;
    setDomainsLoading(true);
    setDomainsError(null);
    try {
      const next = await listStandardDomains();
      if (my !== domainsSeq.current) return;
      setDomains(next);
    } catch (err: unknown) {
      if (my !== domainsSeq.current) return;
      setDomains([]);
      setDomainsError(
        loadErrorText(err, 'dataGovernance.standards.loadFailed', tRef.current),
      );
    } finally {
      if (my === domainsSeq.current) setDomainsLoading(false);
    }
  }, []);

  const loadDomainDetail = useCallback(async (id: string) => {
    const my = ++detailSeq.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const next = await getStandardDomain(id);
      if (my !== detailSeq.current) return;
      setDomainDetail(next);
    } catch (err: unknown) {
      if (my !== detailSeq.current) return;
      setDomainDetail(null);
      setDetailError(
        loadErrorText(err, 'dataGovernance.standards.detailLoadFailed', tRef.current),
      );
    } finally {
      if (my === detailSeq.current) setDetailLoading(false);
    }
  }, []);

  const loadKbSchemas = useCallback(
    async (page: number, pageSize: number, keyword: string) => {
      const my = ++kbSeq.current;
      setKbLoading(true);
      setKbError(null);
      try {
        const next = await listKbSchemas({ keyword, page, pageSize });
        if (my !== kbSeq.current) return;
        setKbItems(next.items);
        setKbTotal(next.total);
      } catch (err: unknown) {
        if (my !== kbSeq.current) return;
        setKbItems([]);
        setKbTotal(0);
        setKbError(
          loadErrorText(err, 'dataGovernance.standards.kbLoadFailed', tRef.current),
        );
      } finally {
        if (my === kbSeq.current) setKbLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadDomains();
  }, [loadDomains]);

  // 切领域：先清空上一份详情，再由序号保证旧响应不回写。
  useEffect(() => {
    if (!selectedDomainId) {
      detailSeq.current += 1;
      setDomainDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    setDomainDetail(null);
    void loadDomainDetail(selectedDomainId);
  }, [selectedDomainId, loadDomainDetail]);

  // 切离模板标准：作废在途请求（返回时由 enterTemplateTab 重新加载详情）。
  useEffect(() => {
    if (activeTab === 'template') return;
    domainsSeq.current += 1;
    detailSeq.current += 1;
    setDomainsLoading(false);
    setDetailLoading(false);
  }, [activeTab]);

  // 知识库应用：进入该 Tab 或分页/关键词变化时取数；切离时丢弃在途响应。
  useEffect(() => {
    if (activeTab !== 'kb') {
      kbSeq.current += 1;
      setKbLoading(false);
      return;
    }
    void loadKbSchemas(kbPage, kbPageSize, kbAppliedKeyword);
  }, [activeTab, kbPage, kbPageSize, kbAppliedKeyword, loadKbSchemas]);

  const enterTemplateTab = () => {
    setActiveTab('template');
    const action = templateEntryAction({
      selectedDomainId,
      domainCount: domains.length,
      domainsError: Boolean(domainsError),
    });
    if (action === 'detail' && selectedDomainId) {
      void loadDomainDetail(selectedDomainId);
    } else if (action === 'domains') {
      // 切去 KB 时在途的领域列表请求已被作废；返回时补取一次。
      void loadDomains();
    }
  };

  const changeTab = (key: string) => {
    const next = key as CatalogTab;
    if (next === 'template') {
      enterTemplateTab();
      return;
    }
    setActiveTab(next);
  };

  const applyKbSearch = () => {
    const keyword = kbKeyword.trim();
    if (kbPage === 1 && keyword === kbAppliedKeyword) {
      // 条件未变：显式刷新，避免 setState 相同值被 React 跳过。
      void loadKbSchemas(1, kbPageSize, keyword);
      return;
    }
    setKbPage(1);
    setKbAppliedKeyword(keyword);
  };

  const viewScenarioFromKb = (id: string) => {
    // 领域名只能取当前 KB 行自己的值：旧 domainDetail 可能属于另一个领域。
    const row = kbSelected;
    const domainId = row?.domainId ?? null;
    // 打开场景抽屉前收起 KB 抽屉，避免两个抽屉叠在一起。
    setKbSelected(null);
    setActiveTab('template');
    setScenarioDomainName(row?.domainName ?? null);
    setScenarioId(id);
    // 先作废在途请求并清空旧领域详情，避免展示上一个领域。
    detailSeq.current += 1;
    setDomainDetail(null);
    setDetailError(null);
    const action = kbEntryDomainAction(selectedDomainId, domainId);
    if (action === 'explicit' && domainId) {
      // 同一领域不会触发 [selectedDomainId] effect，这里显式重取。
      void loadDomainDetail(domainId);
    } else if (action === 'effect' && domainId) {
      // 不同领域：交给 effect（它同样会清空并作废在途请求），避免重复请求。
      setSelectedDomainId(domainId);
    } else {
      // 领域 id 缺失：回到领域列表，不沿用任何旧领域详情。
      setSelectedDomainId(null);
      setDetailLoading(false);
    }
  };

  const domainColumns: ColumnsType<DgStandardDomain> = [
    {
      title: t('dataGovernance.column.name'),
      dataIndex: 'name',
      width: 220,
      ellipsis: true,
    },
    {
      title: t('dataGovernance.standards.description'),
      dataIndex: 'description',
      width: 280,
      ellipsis: true,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.status'),
      dataIndex: 'status',
      width: 110,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.version'),
      dataIndex: 'version',
      width: 90,
      render: (value: number | null) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.scenarioCount'),
      dataIndex: 'scenarioCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.objectCount'),
      dataIndex: 'objectCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.latestPublishedAt'),
      dataIndex: 'latestPublishedAt',
      width: 170,
      render: (value: string | null | undefined) =>
        isUnpublished(value ?? null) ? t('dataGovernance.standards.unpublished') : formatDateTime(value),
    },
  ];

  const scenarioColumns: ColumnsType<DgStandardScenario> = [
    {
      title: t('dataGovernance.column.name'),
      dataIndex: 'name',
      width: 220,
      ellipsis: true,
    },
    {
      title: t('dataGovernance.standards.version'),
      dataIndex: 'version',
      width: 90,
      render: (value: number | null) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.publishedAt'),
      dataIndex: 'publishedAt',
      width: 170,
      render: (value: string | null) =>
        isUnpublished(value) ? t('dataGovernance.standards.unpublished') : formatDateTime(value),
    },
    {
      title: t('dataGovernance.standards.objectCount'),
      dataIndex: 'objectCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.attributeCount'),
      dataIndex: 'attributeCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.relationCount'),
      dataIndex: 'relationCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.constraintCount'),
      dataIndex: 'constraintCount',
      width: 90,
      render: (value: number | undefined) => value ?? '—',
    },
  ];

  const kbColumns: ColumnsType<DgKbSchema> = [
    { title: t('dataGovernance.standards.kb'), dataIndex: 'kbName', width: 180 },
    {
      title: t('dataGovernance.standards.domainScenario'),
      key: 'domainScenario',
      width: 220,
      ellipsis: true,
      // 名称缺失时回退对应 id（有 id 就不显示占位符），两者都无才显示 —。
      render: (_: unknown, row) =>
        `${displayNameOrId(row.domainName, row.domainId)} / ${displayNameOrId(row.scenarioName, row.scenarioId)}`,
    },
    {
      title: t('dataGovernance.standards.bindingStatus'),
      dataIndex: 'bindingStatus',
      width: 200,
      render: (_: unknown, row) => (
        <BindingStatusTag
          bindingStatus={row.bindingStatus}
          bindingScenarioId={row.bindingScenarioId}
          compiledScenarioId={row.compiledScenarioId}
        />
      ),
    },
    {
      title: t('dataGovernance.standards.sourceVersion'),
      dataIndex: 'sourceVersion',
      width: 110,
      render: (value: number | null) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.schemaVersion'),
      dataIndex: 'schemaVersion',
      width: 120,
      render: (value: number | null) => value ?? '—',
    },
    {
      title: t('dataGovernance.standards.schemaMode'),
      dataIndex: 'schemaMode',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.syncStatus'),
      dataIndex: 'syncStatus',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.docStats'),
      key: 'docStats',
      width: 260,
      render: (_: unknown, row) => <DocStatsCell stats={row.docStats} />,
    },
  ];

  const templateContent = selectedDomainId ? (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Button icon={<ArrowLeftOutlined />} onClick={() => setSelectedDomainId(null)}>
          {t('dataGovernance.standards.back')}
        </Button>
        <Breadcrumb
          items={[
            {
              title: (
                <Button
                  type="link"
                  style={{ padding: 0 }}
                  onClick={() => setSelectedDomainId(null)}
                >
                  {t('dataGovernance.standards.title')}
                </Button>
              ),
            },
            { title: domainDetail?.domain.name ?? selectedDomainId },
          ]}
        />
        <Tooltip title={t('dataGovernance.standards.refresh')}>
          <Button
            aria-label={t('dataGovernance.standards.refresh')}
            icon={<ReloadOutlined />}
            loading={detailLoading}
            onClick={() => void loadDomainDetail(selectedDomainId)}
          />
        </Tooltip>
      </Space>
      {detailLoading ? (
        <Skeleton active />
      ) : detailError ? (
        <Alert
          type="error"
          showIcon
          title={detailError}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadDomainDetail(selectedDomainId)}
            >
              {t('dataGovernance.standards.retry')}
            </Button>
          }
        />
      ) : !domainDetail ? (
        <Empty description={t('dataGovernance.standards.emptyScenarios')} />
      ) : (
        <Table<DgStandardScenario>
          rowKey="id"
          size="middle"
          columns={scenarioColumns}
          dataSource={domainDetail.scenarios}
          pagination={false}
          scroll={{ x: 840 }}
          onRow={(record) => ({
            onClick: () => {
              setScenarioDomainName(domainDetail?.domain.name ?? null);
              setScenarioId(record.id);
            },
            style: { cursor: 'pointer' },
          })}
          locale={{
            emptyText: (
              <Empty description={t('dataGovernance.standards.emptyScenarios')} />
            ),
          }}
        />
      )}
    </Space>
  ) : domainsError ? (
    <Alert
      type="error"
      showIcon
      title={domainsError}
      action={
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void loadDomains()}>
          {t('dataGovernance.standards.retry')}
        </Button>
      }
    />
  ) : (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Tooltip title={t('dataGovernance.standards.refresh')}>
          <Button
            aria-label={t('dataGovernance.standards.refresh')}
            icon={<ReloadOutlined />}
            loading={domainsLoading}
            onClick={() => void loadDomains()}
          />
        </Tooltip>
      </div>
      <Table<DgStandardDomain>
        rowKey="id"
        size="middle"
        loading={domainsLoading}
        columns={domainColumns}
        dataSource={domains}
        pagination={false}
        scroll={{ x: 1050 }}
        onRow={(record) => ({
          onClick: () => setSelectedDomainId(record.id),
          style: { cursor: 'pointer' },
        })}
        locale={{
          emptyText: <Empty description={t('dataGovernance.standards.emptyDomains')} />,
        }}
      />
    </Space>
  );

  const kbContent = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Input
          allowClear
          value={kbKeyword}
          placeholder={t('dataGovernance.standards.keywordPlaceholder')}
          onChange={(event) => setKbKeyword(event.target.value)}
          onPressEnter={applyKbSearch}
          style={{ width: 240 }}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={applyKbSearch}>
          {t('dataGovernance.filter.search')}
        </Button>
        <Tooltip title={t('dataGovernance.standards.refresh')}>
          <Button
            aria-label={t('dataGovernance.standards.refresh')}
            icon={<ReloadOutlined />}
            loading={kbLoading}
            onClick={() => void loadKbSchemas(kbPage, kbPageSize, kbAppliedKeyword)}
          />
        </Tooltip>
      </Space>

      {kbError ? (
        <Alert
          type="error"
          showIcon
          title={kbError}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void loadKbSchemas(kbPage, kbPageSize, kbAppliedKeyword)}
            >
              {t('dataGovernance.standards.retry')}
            </Button>
          }
        />
      ) : (
        <Table<DgKbSchema>
          rowKey="kbId"
          size="middle"
          loading={kbLoading}
          columns={kbColumns}
          dataSource={kbItems}
          scroll={{ x: 1350 }}
          onRow={(record) => ({
            onClick: () => setKbSelected(record),
            style: { cursor: 'pointer' },
          })}
          locale={{
            emptyText: <Empty description={t('dataGovernance.standards.emptyKb')} />,
          }}
          pagination={{
            current: kbPage,
            pageSize: kbPageSize,
            total: kbTotal,
            showSizeChanger: true,
            pageSizeOptions: DG_PAGE_SIZE_OPTIONS,
            onChange: (nextPage, nextSize) => {
              // 改每页条数时回到第 1 页，避免旧页码超出新页数后落到空页。
              setKbPage(pageAfterSizeChange(kbPageSize, nextPage, nextSize));
              setKbPageSize(nextSize);
            },
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}
    </Space>
  );

  return (
    <>
      <Tabs
        activeKey={activeTab}
        onChange={changeTab}
        items={[
          {
            key: 'template',
            label: t('dataGovernance.standards.templateTab'),
            children: templateContent,
          },
          {
            key: 'kb',
            label: t('dataGovernance.standards.kbTab'),
            children: kbContent,
          },
        ]}
      />

      <StandardScenarioDrawer
        open={Boolean(scenarioId)}
        scenarioId={scenarioId}
        domainName={scenarioDomainName}
        onClose={() => {
          setScenarioId(null);
          setScenarioDomainName(null);
        }}
      />

      <KbSchemaDrawer
        open={Boolean(kbSelected)}
        row={kbSelected}
        onClose={() => setKbSelected(null)}
        onViewScenario={viewScenarioFromKb}
      />
    </>
  );
}
