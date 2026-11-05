/**
 * 知识库设置 →「血缘」页签（列表 + 上下游列级下钻 + 手工 CRUD）。
 *
 * 每一行是一条**列级**血缘边：`from_table.from_column → to_table.to_column`。
 * 端点（`表.列` 全串）各是一枚可点的 chip：
 *   - 点**左端**（来源列）⇒ 看它的**上游**：筛出 `to` 端命中该节点的边（谁流向它）；
 *   - 点**右端**（去向列）⇒ 看它的**下游**：筛出 `from` 端命中该节点的边（它流向谁）。
 * 比较一律用 `表.列` 全串 —— **同一张表的不同列是不同节点**，不能只看表名。
 *
 * 下钻是**纯前端**筛选：列表按源整取（一个源的血缘边通常在几十条量级），
 * 没有分页压力，也就不为下钻单开接口。切数据源 = 一次新查询 + 清掉下钻。
 *
 * ⚠️ `unknown` 必须显式（设计硬要求）：
 *   - 该源没有任何边 ⇒ 「该数据源还没有血缘边 —— 可以在下方手工登记，或等 ETL 集成接入来源」，
 *     **不许**渲染成「无上游」；
 *   - 下钻到某节点后没有边 ⇒ 「没有到/自该节点的血缘边」，**不是**「该节点无上下游」。
 *     查不到 ≠ 没有：也可能来源尚未接入。
 *
 * 四态齐：loading / error（可重试）/ empty / success，另有「无数据库类型数据源」的提示态。
 *
 * 写入面仅在 `canWrite` 时出现：顶部「新增血缘边」+ 每行「编辑 / 删除（Popconfirm 二次确认）」。
 * 失败提示**原样透出后端 message**（400 的「表 X 上没有列 Y」「该血缘边已存在（同一四元组）」
 * 是用户唯一能看懂「为什么写不进去」的地方）—— 不许吞成通用文案。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Breadcrumb,
  Button,
  Empty,
  message,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowRightOutlined,
  DeleteOutlined,
  EditOutlined,
  NodeIndexOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import {
  createLineageEdge,
  deleteLineageEdge,
  listLineageEdges,
  updateLineageEdge,
  type LineageEdge,
} from '@/api/lineage';
import type { DataSourceConfig } from '@/types/domainKnowledge';
import { filterByNode, nodeKey, type DrillDir } from './lineageModel';
import { anchorTables, lineageRevision } from './lineageGraphModel';
import LineageCoverageBar from './LineageCoverageBar';
import LineageEdgeModal, { type LineageEdgeForm } from './LineageEdgeModal';
import LineageForbiddenView from './LineageForbiddenView';
import LineageGraphView from './LineageGraphView';
import LineageImpactView from './LineageImpactView';

const { Text } = Typography;

interface DrillState {
  /** `表.列` 全串（见 `nodeKey`） */
  node: string;
  dir: DrillDir;
}

/** 页签视图：列表 / 只读多跳画布 / 禁止同查表对 / 影响分析。 */
type LineageView = 'list' | 'graph' | 'forbidden' | 'impact';

/** `origin` 档 → 标签文案 + 颜色（取值见后端 `paths.ORIGIN_CONFIDENCE` 的键）。 */
const ORIGIN_TAGS: Record<string, { key: string; color: string }> = {
  declared: { key: 'domainKnowledge.lineage.originDeclared', color: 'green' },
  manual: { key: 'domainKnowledge.lineage.originManual', color: 'blue' },
  comment: { key: 'domainKnowledge.lineage.originComment', color: 'gold' },
  platform: { key: 'domainKnowledge.lineage.originPlatform', color: 'purple' },
  value: { key: 'domainKnowledge.lineage.originValue', color: 'cyan' },
  derived: { key: 'domainKnowledge.lineage.originDerived', color: 'default' },
};

interface Props {
  kbId: string;
  /**
   * KB 写权限（权限矩阵，口径同 `AbstractSchemaTab`）。
   * 为 false 时**不渲染任何写入入口**（新增 / 编辑 / 删除）—— 权限不是靠后端拦，
   * 前端也不给按钮。
   */
  canWrite?: boolean;
  /** 固定到某个数据源（从数据源卡片进入时用）：隐藏选择器，只加载该源。 */
  fixedDataSourceId?: string;
}

export default function LineageTab({ kbId, canWrite = false,
                                     fixedDataSourceId }: Props) {
  const { t } = useTranslation();

  /**
   * 视图切换：列表（默认，行为与写入面完全不变）/ 画布（只读，惰性挂载）。
   * 画布视图**保留**数据源选择与「新增血缘边」（写入口仍按 `canWrite` 门控），
   * 隐藏的是列表表格、下钻面包屑与列表提示；列表的下钻状态原样保留，
   * 切回列表不受影响。
   */
  const [view, setView] = useState<LineageView>('list');

  // ── 数据源（只列 database 源；与 canWrite 无关）────────────────
  const [sources, setSources] = useState<DataSourceConfig[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [sourcesError, setSourcesError] = useState('');
  const [selectedDsId, setSelectedDsId] = useState<string>();

  // ── 血缘边列表 + 下钻 ─────────────────────────────────────────
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  // 初始就是 loading：数据源解析出默认源的那一帧到拉边 effect 执行之间，
  // 若先按 `false` 渲染，会闪一帧「该数据源还没有血缘边」的假空态。
  const [edgesLoading, setEdgesLoading] = useState(true);
  const [edgesError, setEdgesError] = useState('');
  const [drill, setDrill] = useState<DrillState | null>(null);
  /**
   * 每次成功拉边递增：页头「刷新」即便后端内容一字未变，也要让画布重查
   * （刷新是用户显式动作，画布不该停在旧结果上）。
   */
  const [edgesRevisionSeq, setEdgesRevisionSeq] = useState(0);

  // ── 写入面（仅 canWrite）───────────────────────────────────────
  const [modalOpen, setModalOpen] = useState(false);
  /** 传入 = 编辑态；null = 新增态 */
  const [editingEdge, setEditingEdge] = useState<LineageEdge | null>(null);
  const [submitting, setSubmitting] = useState(false);
  /** 正在删除的边 id（只让那一行的删除按钮转圈，不整表锁死） */
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const loadSources = useCallback(async () => {
    setSourcesLoading(true);
    setSourcesError('');
    try {
      const list = await getDomainKnowledgeDataSources(kbId, t);
      // 血缘只对结构化源有意义：文件 / API 源没有「表.列」可映射，
      // 列进来只会永远空列表，让人误以为是「没有边」。
      const databases = list.filter((ds) => ds.accessType === 'database');
      const scoped = fixedDataSourceId
        ? databases.filter((ds) => ds.id === fixedDataSourceId) : databases;
      setSources(scoped);
      // 默认选第一个；刷新后原选择仍在则保留（不因一次刷新重置用户的选择）
      setSelectedDsId((prev) =>
        fixedDataSourceId
          || (prev && scoped.some((ds) => ds.id === prev) ? prev : scoped[0]?.id),
      );
    } catch (e: any) {
      setSourcesError(e?.message || t('domainKnowledge.lineage.loadDataSourcesFailed'));
    } finally {
      setSourcesLoading(false);
    }
  }, [kbId, t, fixedDataSourceId]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  /** 请求序号：快速切源时丢掉旧源的迟到响应，避免 A 的边显示在 B 下面。 */
  const edgesReqRef = useRef(0);
  const loadEdges = useCallback(async () => {
    const req = ++edgesReqRef.current;
    if (!selectedDsId) {
      // 还没有默认源（数据源仍在加载）：**保持 loading**，不要落到 false。
      // 否则「数据源落地 → 边拉取 effect 执行」之间会有一帧 loading=false，
      // 覆盖率条会先按空 revision 挂载并白白请求一次，边落地后又请求一次。
      setEdges([]);
      setEdgesError('');
      return;
    }
    setEdgesLoading(true);
    setEdgesError('');
    try {
      const data = await listLineageEdges(kbId, selectedDsId);
      if (edgesReqRef.current === req) {
        setEdges(data.items);
        setEdgesRevisionSeq((seq) => seq + 1);
      }
    } catch (e: any) {
      if (edgesReqRef.current === req) {
        setEdgesError(e?.message || t('domainKnowledge.lineage.loadFailed'));
      }
    } finally {
      if (edgesReqRef.current === req) setEdgesLoading(false);
    }
  }, [kbId, selectedDsId, t]);

  // 切源 = 一次新查询，且**清掉下钻**：面包屑上的节点属于上一个源，
  // 留着它只会得到「没有到/自该节点的血缘边」这种误导性空态。
  useEffect(() => {
    setDrill(null);
    void loadEdges();
  }, [loadEdges]);

  const openCreate = () => {
    setEditingEdge(null);
    setModalOpen(true);
  };

  const openEdit = (edge: LineageEdge) => {
    setEditingEdge(edge);
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setEditingEdge(null);
  };

  const submit = async (v: LineageEdgeForm) => {
    setSubmitting(true);
    try {
      if (editingEdge) {
        // 编辑载荷**不含 `data_source_id`**：边不能换源（后端见到不同的源会 1001 拒绝）。
        await updateLineageEdge(kbId, editingEdge.id, v);
      } else {
        if (!selectedDsId) return;
        // `data_source_id` 由页签当前选中的源带入，不给用户选（表名是物理表名）。
        await createLineageEdge(kbId, { ...v, data_source_id: selectedDsId });
      }
      await loadEdges();
      closeModal();
    } catch (err: any) {
      // ⚠️ 原样透出后端 message：400 说的是「表 X 上没有列 Y」「该血缘边已存在（同一四元组）」
      // —— 那是用户唯一能看懂「为什么写不进去」的地方。吞成「操作失败」等于让人无解可查。
      message.error(
        err?.message ||
          t(editingEdge ? 'domainKnowledge.lineage.saveFailed' : 'domainKnowledge.lineage.createFailed'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async (edge: LineageEdge) => {
    setDeletingId(edge.id);
    try {
      await deleteLineageEdge(kbId, edge.id);
      await loadEdges();
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.lineage.deleteFailed'));
    } finally {
      setDeletingId(null);
    }
  };

  const visibleEdges = useMemo(
    () => (drill ? filterByNode(edges, drill.node, drill.dir) : edges),
    [edges, drill],
  );

  /** 画布锚点候选：当前源血缘边里出现过的全部表名（去重 + 码点排序）。 */
  const tables = useMemo(() => anchorTables(edges), [edges]);

  /** 影响分析的列下拉建议：表 → 该表在边集里出现过的列。 */
  const columnsByTable = useMemo(() => {
    const map: Record<string, Set<string>> = {};
    const add = (table: string, column: string) => {
      if (!table || !column) return;
      const bucket = map[table] ?? new Set<string>();
      bucket.add(column);
      map[table] = bucket;
    };
    for (const edge of edges) {
      add(edge.from_table, edge.from_column);
      add(edge.to_table, edge.to_column);
    }
    return Object.fromEntries(
      Object.entries(map).map(([table, columns]) => [table, [...columns].sort()]),
    );
  }, [edges]);

  /**
   * 画布重查依据：表集变化（新增/删除涉及新表）或边内容变化（**同一表对的
   * 第二条列级边**只改边数/时间戳，表集不变）都必须触发重查。序号兜底页头刷新。
   */
  const graphRevision = useMemo(
    () => `${edgesRevisionSeq}:${lineageRevision(edges)}`,
    [edges, edgesRevisionSeq],
  );

  /** 端点 chip：左端 → 上游，右端 → 下游（见文件头）。 */
  const renderEndpoint = (edge: LineageEdge, side: 'from' | 'to') => {
    const node =
      side === 'from'
        ? nodeKey(edge.from_table, edge.from_column)
        : nodeKey(edge.to_table, edge.to_column);
    const dir: DrillDir = side === 'from' ? 'up' : 'down';
    return (
      <Tag
        color={side === 'from' ? 'blue' : 'geekblue'}
        style={{ cursor: 'pointer', marginInlineEnd: 0, fontFamily: 'Consolas, monospace' }}
        title={
          side === 'from'
            ? t('domainKnowledge.lineage.clickUpstream')
            : t('domainKnowledge.lineage.clickDownstream')
        }
        onClick={() => setDrill({ node, dir })}
      >
        {node}
      </Tag>
    );
  };

  const baseColumns: ColumnsType<LineageEdge> = [
    {
      title: t('domainKnowledge.lineage.fromEndpoint'),
      key: 'from',
      render: (_: unknown, edge) => renderEndpoint(edge, 'from'),
    },
    {
      title: '',
      key: 'arrow',
      width: 48,
      align: 'center',
      render: () => <ArrowRightOutlined style={{ color: '#94a3b8' }} />,
    },
    {
      title: t('domainKnowledge.lineage.toEndpoint'),
      key: 'to',
      render: (_: unknown, edge) => renderEndpoint(edge, 'to'),
    },
    {
      title: t('domainKnowledge.lineage.origin'),
      dataIndex: 'origin',
      key: 'origin',
      width: 170,
      render: (origin: string, record: LineageEdge) => {
        const meta = ORIGIN_TAGS[origin];
        // 未知档位原样显示：不猜、也不吞（后端新增档位时这里要能看见它）
        return (
          <>
            <Tag color={meta?.color ?? 'default'}>{meta ? t(meta.key) : origin}</Tag>
            {/* 失效标记是读时计算（platform 来源超过 30 天未被再次确认）；只提示，不删边 */}
            {record.stale && (
              <Tag color="volcano" title={t('domainKnowledge.lineage.staleHint')}>
                {t('domainKnowledge.lineage.stale')}
              </Tag>
            )}
          </>
        );
      },
    },
    {
      title: t('domainKnowledge.lineage.note'),
      dataIndex: 'note',
      key: 'note',
      render: (note?: string | null) =>
        note ? <span>{note}</span> : <span style={{ color: '#cbd5e1' }}>—</span>,
    },
  ];

  // 写入入口只在 canWrite 时存在：没有权限连按钮都不给（不是靠后端拦）。
  const columns: ColumnsType<LineageEdge> = canWrite
    ? [
        ...baseColumns,
        {
          title: t('domainKnowledge.lineage.actions'),
          key: 'actions',
          width: 150,
          render: (_: unknown, edge) => (
            <Space size={0}>
              <Button
                type="text"
                size="small"
                icon={<EditOutlined />}
                onClick={() => openEdit(edge)}
              >
                {t('common.edit')}
              </Button>
              {/* 删除必须二次确认：血缘边是模型路径搜索的输入，误删会静默改变问数结果 */}
              <Popconfirm
                title={t('domainKnowledge.lineage.deleteConfirm')}
                onConfirm={() => void remove(edge)}
                okText={t('common.confirm')}
                cancelText={t('common.cancel')}
              >
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  loading={deletingId === edge.id}
                >
                  {t('common.delete')}
                </Button>
              </Popconfirm>
            </Space>
          ),
        },
      ]
    : baseColumns;

  const breadcrumbItems = drill
    ? [
        {
          title: (
            <a onClick={() => setDrill(null)} style={{ color: '#64748b' }}>
              {t('domainKnowledge.lineage.all')}
            </a>
          ),
        },
        {
          title: (
            <span style={{ color: '#0b2b5c', fontWeight: 500, fontFamily: 'Consolas, monospace' }}>
              {drill.node}
            </span>
          ),
        },
        {
          title: (
            <span style={{ color: '#0b2b5c' }}>
              {drill.dir === 'up'
                ? t('domainKnowledge.lineage.upstream')
                : t('domainKnowledge.lineage.downstream')}
            </span>
          ),
        },
      ]
    : [];

  /** 拉边失败的告警块：列表与画布两个视图共用（画布自己也靠这份边取锚点候选）。 */
  const edgesErrorAlert = (
    <Alert
      type="error"
      showIcon
      message={t('domainKnowledge.lineage.loadFailed')}
      description={edgesError}
      action={
        <Button size="small" onClick={() => void loadEdges()}>
          {t('common.retry')}
        </Button>
      }
    />
  );

  // ── 整页态：数据源加载 / 失败 / 无数据库源 ─────────────────────
  if (sourcesLoading) {
    return (
      <div style={{ padding: 24, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  if (sourcesError) {
    return (
      <Alert
        type="error"
        showIcon
        message={t('domainKnowledge.lineage.loadDataSourcesFailed')}
        description={sourcesError}
        action={
          <Button size="small" onClick={() => void loadSources()}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  if (sources.length === 0) {
    return <Empty description={t('domainKnowledge.lineage.noDatabaseSource')} />;
  }

  return (
    <div>
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <NodeIndexOutlined /> {t('domainKnowledge.lineage.title')}
          {view !== 'forbidden' && !edgesLoading && !edgesError && (
            <span className="yx-kb-chip-blue">
              {view === 'list' && drill
                ? // 下钻态明确写「筛选结果 / 共」：同一个位置若只显示筛选后的条数，
                  // 会被读成「整源总数」。画布视图没有下钻，一律显示整源条数。
                  t('domainKnowledge.lineage.countFiltered', {
                    shown: visibleEdges.length,
                    total: edges.length,
                  })
                : t('domainKnowledge.lineage.count', { total: edges.length })}
            </span>
          )}
        </h3>
        <Space>
          {/* 视图切换与数据源选择同排：两者都是「看哪一份数据」的入口 */}
          <Segmented
            value={view}
            onChange={(value) => setView(value as LineageView)}
            options={[
              { value: 'list', label: t('domainKnowledge.lineage.viewList') },
              { value: 'graph', label: t('domainKnowledge.lineage.viewGraph') },
              { value: 'forbidden', label: t('domainKnowledge.lineage.viewForbidden') },
              { value: 'impact', label: t('domainKnowledge.lineage.viewImpact') },
            ]}
          />
          {!fixedDataSourceId && (
            <>
              <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.dataSource')}</span>
              <Select
                value={selectedDsId}
                onChange={(value) => {
                  // 同一次事件里先把 loading 置真：否则这次 render 会先用**上一个源**的
                  // edges 画一帧（拉边 effect 要等到渲染之后才跑）。
                  setEdgesLoading(true);
                  setSelectedDsId(value);
                }}
                options={sources.map((ds) => ({ value: ds.id, label: ds.name }))}
                placeholder={t('domainKnowledge.lineage.dataSourcePlaceholder')}
                style={{ minWidth: 240 }}
                showSearch
                optionFilterProp="label"
              />
            </>
          )}
          {view !== 'forbidden' && (
            <Button
              icon={<ReloadOutlined />}
              loading={edgesLoading}
              onClick={() => void loadEdges()}
            >
              {t('common.refresh')}
            </Button>
          )}
          {canWrite && view !== 'forbidden' && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              {t('domainKnowledge.lineage.addEdge')}
            </Button>
          )}
        </Space>
      </div>

      {/* 覆盖率条：列表 / 画布 / 影响分析共用（禁止同查视图不读边，不显示）。
          `key={selectedDsId}` 让切源彻底重挂（清掉上一个源的数字）；
          `revision={graphRevision}`：切源与边写成功后自动重查。
          **只在边首载成功后挂载**：否则进入页签/切源时会先随 `revision` 初值发一次，
          边落地后 `graphRevision` 变化再发一次 —— 同一个源白打两枪。 */}
      {selectedDsId && view !== 'forbidden' && !edgesLoading && !edgesError && (
        <LineageCoverageBar
          key={selectedDsId}
          kbId={kbId}
          dsId={selectedDsId}
          revision={graphRevision}
        />
      )}

      {/* 提示文案描述的是列表的端点下钻，画布视图不适用（画布有自己的控件图例） */}
      {view === 'list' && (
        <p className="yx-kb-graph-desc">
          {t(canWrite ? 'domainKnowledge.lineage.writableHint' : 'domainKnowledge.lineage.readonlyHint')}
        </p>
      )}

      {view === 'list' && drill && (
        <div className="yx-kb-flex-between" style={{ marginBottom: 12 }}>
          <Breadcrumb items={breadcrumbItems} />
          <Button size="small" onClick={() => setDrill(null)}>
            {t('domainKnowledge.lineage.backToAll')}
          </Button>
        </div>
      )}

      {view === 'forbidden' ? (
        selectedDsId ? (
          <LineageForbiddenView key={selectedDsId} kbId={kbId} dsId={selectedDsId}
                                canWrite={canWrite} />
        ) : null
      ) : view === 'graph' ? (
        // 画布视图：边列表仍在拉取时先转圈（画布锚点候选来自它，空数组会被
        // 误读成「该源没有血缘边」）；随后把整份边集交给画布组件。
        // `key={selectedDsId}`：切源时整块重挂，锚点/方向/跳数一并回到默认，
        // 也避免拿上一个源的锚点向新源发一次注定被丢弃的请求。
        edgesLoading ? (
          <div style={{ padding: 24, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : edgesError ? (
          edgesErrorAlert
        ) : selectedDsId ? (
          <LineageGraphView
            key={selectedDsId}
            kbId={kbId}
            dsId={selectedDsId}
            tables={tables}
            revision={graphRevision}
          />
        ) : null
      ) : view === 'impact' ? (
        // 影响分析：锚点候选来自当前源的边集，边仍在拉取/失败时与画布同口径
        // （转圈 / 原样透出错误），否则切源瞬间会拿到上一个源的 tables。
        // `key={selectedDsId}`：切源时整块重挂，锚点/方向/跳数回到默认；
        // 边内容变化不重挂（用户点「分析」再查）。
        edgesLoading ? (
          <div style={{ padding: 24, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : edgesError ? (
          edgesErrorAlert
        ) : selectedDsId ? (
          <LineageImpactView
            key={selectedDsId}
            kbId={kbId}
            dsId={selectedDsId}
            tables={tables}
            columnsByTable={columnsByTable}
          />
        ) : null
      ) : edgesLoading ? (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin />
        </div>
      ) : edgesError ? (
        edgesErrorAlert
      ) : visibleEdges.length === 0 ? (
        drill ? (
          // 下钻空态：只说「到/自该节点没有边」，不说「该节点无上下游」——
          // 查不到 ≠ 没有（来源可能尚未接入）。见文件头。
          <Empty
            description={
              <span>
                {t('domainKnowledge.lineage.drillEmpty')}
                <br />
                <Text type="secondary">{t('domainKnowledge.lineage.drillEmptyHint')}</Text>
              </span>
            }
          />
        ) : (
          // 整源空态：必须点明「手工登记 / ETL 接入」两条来源，不许渲染成「无上游」
          <Empty description={t('domainKnowledge.lineage.empty')} />
        )
      ) : (
        <Table<LineageEdge>
          rowKey="id"
          columns={columns}
          dataSource={visibleEdges}
          pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
          size="middle"
        />
      )}

      {canWrite && (
        <LineageEdgeModal
          open={modalOpen}
          edge={editingEdge}
          submitting={submitting}
          onCancel={closeModal}
          onSubmit={(v) => void submit(v)}
        />
      )}
    </div>
  );
}
