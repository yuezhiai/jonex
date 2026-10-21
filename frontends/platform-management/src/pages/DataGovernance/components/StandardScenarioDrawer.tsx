import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CopyOutlined, ReloadOutlined } from '@ant-design/icons';
import copyToClipboard from 'copy-to-clipboard';
import { useTranslation } from 'react-i18next';
import { formatDateTime, usePermission } from '@jonex/shared-lib';
import {
  diffScenarioVersions,
  getStandardCompliance,
  getStandardReferences,
  getStandardScenario,
  listScenarioVersions,
  rollbackScenarioVersion,
} from '@/api/standardCatalog';
import type {
  DgComplianceEntity,
  DgComplianceResult,
  DgEntityDiff,
  DgFieldChange,
  DgKbComplianceItem,
  DgScenarioVersion,
  DgScenarioVersionDiff,
  DgStandardAttribute,
  DgStandardConstraint,
  DgStandardObject,
  DgStandardReference,
  DgStandardReferencesResult,
  DgStandardRelation,
  DgStandardScenario,
} from '../standardTypes';
import {
  isUnpublished,
  loadErrorText,
  scenarioRefreshMode,
  shouldKeepImpact,
} from '../standardUtils';
import { BindingStatusTag, DocStatsCell } from './StandardCatalogBits';

interface Props {
  open: boolean;
  scenarioId: string | null;
  /** 领域名由上层列表带入；缺失时头部回退展示 scenario.domainId。 */
  domainName?: string | null;
  onClose: () => void;
}

interface ScenarioData {
  scenario: DgStandardScenario;
  objects: DgStandardObject[];
  relations: DgStandardRelation[];
  constraints: DgStandardConstraint[];
}

type TabKey =
  | 'objects'
  | 'relations'
  | 'constraints'
  | 'impact'
  | 'versions'
  | 'compliance';

/** 引用结果的查询范围：默认是场景，点对象/属性的按钮后临时收窄。 */
interface RefScope {
  type: 'scenario' | 'object' | 'attribute';
  id: string;
  label: string;
}

/**
 * 标准场景抽屉：模板标准的只读视图。
 *
 * 目录展示的是**当前工作副本**，version/hash 为最近发布值，两者可能不一致；
 * 场景级别的引用 KB 在打开时加载，对象/属性维度的引用按需加载并复用同一块区域。
 */
export default function StandardScenarioDrawer({
  open,
  scenarioId,
  domainName,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const { permissions } = usePermission();
  const canRollback =
    permissions.includes('tenant:admin') || permissions.includes('platform:admin');
  // 异步回调通过 ref 读 t：语言切换不应触发场景重新加载。
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [data, setData] = useState<ScenarioData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 静默刷新（refresh 按钮）失败时的非阻断提示：不覆盖已有内容。
  const [refreshError, setRefreshError] = useState<string | null>(null);
  // 刷新按钮独立 loading：静默刷新不会置 loading，但仍需防止连点叠加。
  const [refreshing, setRefreshing] = useState(false);
  const refreshingRef = useRef(false);
  const [activeTab, setActiveTab] = useState<TabKey>('objects');

  const [impact, setImpact] = useState<DgStandardReferencesResult | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [refScope, setRefScope] = useState<RefScope | null>(null);

  // 发布版本历史与 diff（第 6 项第二增量）：与场景详情各自独立代际。
  const [versions, setVersions] = useState<DgScenarioVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [diffFrom, setDiffFrom] = useState<number | null>(null);
  const [diffTo, setDiffTo] = useState<number | null>(null);
  const [diff, setDiff] = useState<DgScenarioVersionDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const versionsSeq = useRef(0);
  const diffSeq = useRef(0);

  // 落标校验（标准侧第二增量）
  const [compliance, setCompliance] = useState<DgComplianceResult | null>(null);
  const [complianceLoading, setComplianceLoading] = useState(false);
  const [complianceError, setComplianceError] = useState<string | null>(null);
  const complianceSeq = useRef(0);

  // 场景详情与引用结果各自用序号防竞态：切场景/关闭时旧响应一律丢弃。
  const scenarioSeq = useRef(0);
  const refsSeq = useRef(0);
  // 刷新代际：切场景/关抽屉会作废旧刷新，避免旧请求复位新刷新的按钮态。
  const refreshSeq = useRef(0);

  const loadScenario = useCallback(async (id: string, silent = false) => {
    const my = ++scenarioSeq.current;
    if (!silent) setLoading(true);
    setError(null);
    setRefreshError(null);
    try {
      const next = await getStandardScenario(id);
      if (my !== scenarioSeq.current) return;
      setData(next);
    } catch (err: unknown) {
      if (my !== scenarioSeq.current) return;
      const text = loadErrorText(
        err,
        'dataGovernance.standards.scenarioLoadFailed',
        tRef.current,
      );
      if (silent) {
        // 静默刷新失败：保留上一份内容，只给非阻断提示（避免整块内容被错误态顶掉）。
        setRefreshError(text);
      } else {
        setData(null);
        setError(text);
      }
    } finally {
      // 按序号收口：静默结束也清一次 loading（静默开始时未置位，清掉无副作用），
      // 避免初始加载/错误态点刷新后永久停在 Skeleton。
      if (my === scenarioSeq.current) setLoading(false);
    }
  }, []);

  const loadReferences = useCallback(async (scope: RefScope, silent = false) => {
    const my = ++refsSeq.current;
    if (!silent) setImpactLoading(true);
    setImpactError(null);
    setRefreshError(null);
    try {
      const next = await getStandardReferences(scope.type, scope.id);
      if (my !== refsSeq.current) return;
      setImpact(next);
    } catch (err: unknown) {
      if (my !== refsSeq.current) return;
      const text = loadErrorText(
        err,
        'dataGovernance.standards.referencesLoadFailed',
        tRef.current,
      );
      if (silent) {
        // 已有影响数据：静默刷新失败保留旧结果，只给非阻断提示。
        setRefreshError(text);
      } else {
        setImpact(null);
        setImpactError(text);
      }
    } finally {
      if (my === refsSeq.current) setImpactLoading(false);
    }
  }, []);

  const loadVersions = useCallback(async (id: string) => {
    const my = ++versionsSeq.current;
    setVersionsLoading(true);
    setVersionsError(null);
    try {
      const next = await listScenarioVersions(id);
      if (my !== versionsSeq.current) return;
      setVersions(next.items);
      // 默认选中最近两版；不足两版时清空对比选择。
      setDiffTo(next.items[0]?.version ?? null);
      setDiffFrom(next.items[1]?.version ?? null);
      setDiff(null);
      setDiffError(null);
    } catch (err: unknown) {
      if (my !== versionsSeq.current) return;
      setVersions([]);
      setVersionsError(
        loadErrorText(
          err,
          'dataGovernance.standards.versions.loadFailed',
          tRef.current,
        ),
      );
    } finally {
      if (my === versionsSeq.current) setVersionsLoading(false);
    }
  }, []);

  const loadDiff = useCallback(
    async (id: string, from: number | null, to: number | null) => {
      if (from === null || to === null) return;
      const my = ++diffSeq.current;
      setDiffLoading(true);
      setDiffError(null);
      try {
        const next = await diffScenarioVersions(id, from, to);
        if (my !== diffSeq.current) return;
        setDiff(next);
      } catch (err: unknown) {
        if (my !== diffSeq.current) return;
        setDiff(null);
        setDiffError(
          loadErrorText(
            err,
            'dataGovernance.standards.versions.diffFailed',
            tRef.current,
          ),
        );
      } finally {
        if (my === diffSeq.current) setDiffLoading(false);
      }
    },
    [],
  );

  const loadCompliance = useCallback(async (id: string) => {
    const my = ++complianceSeq.current;
    setComplianceLoading(true);
    setComplianceError(null);
    try {
      const next = await getStandardCompliance(id);
      if (my !== complianceSeq.current) return;
      setCompliance(next);
    } catch (err: unknown) {
      if (my !== complianceSeq.current) return;
      setCompliance(null);
      setComplianceError(
        loadErrorText(
          err,
          'dataGovernance.standards.compliance.loadFailed',
          tRef.current,
        ),
      );
    } finally {
      if (my === complianceSeq.current) setComplianceLoading(false);
    }
  }, []);

  const handleRollback = async (version: number) => {
    if (!scenarioId) return;
    try {
      const result = await rollbackScenarioVersion(scenarioId, version);
      message.success(
        tRef.current('dataGovernance.standards.versions.rollbackDone', {
          from: version,
          to: result.publishedVersion ?? '—',
        }),
      );
      await loadScenario(scenarioId);
      await loadVersions(scenarioId);
      await loadCompliance(scenarioId);
      void loadReferences({ type: 'scenario', id: scenarioId, label: '' });
    } catch (err: unknown) {
      message.error(
        loadErrorText(
          err,
          'dataGovernance.standards.versions.rollbackFailed',
          tRef.current,
        ),
      );
    }
  };

  // 打开/切场景：先作废在途请求并清空上一份展示，再并行取场景与场景级引用。
  useEffect(() => {
    scenarioSeq.current += 1;
    refsSeq.current += 1;
    setData(null);
    setError(null);
    setRefreshError(null);
    setActiveTab('objects');
    setImpact(null);
    setImpactError(null);
    setImpactLoading(false);
    setRefScope(null);
    versionsSeq.current += 1;
    diffSeq.current += 1;
    setVersions([]);
    setVersionsLoading(false);
    setVersionsError(null);
    setDiff(null);
    setDiffError(null);
    setDiffLoading(false);
    setDiffFrom(null);
    setDiffTo(null);
    complianceSeq.current += 1;
    setCompliance(null);
    setComplianceLoading(false);
    setComplianceError(null);
    refreshSeq.current += 1;
    refreshingRef.current = false;
    setRefreshing(false);
    if (!open || !scenarioId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    void loadScenario(scenarioId);
    void loadReferences({ type: 'scenario', id: scenarioId, label: '' });
    void loadVersions(scenarioId);
    void loadCompliance(scenarioId);
  }, [open, scenarioId, loadScenario, loadReferences, loadVersions, loadCompliance]);

  const refresh = () => {
    if (!open || !scenarioId || refreshingRef.current) return;
    const my = ++refreshSeq.current;
    refreshingRef.current = true;
    setRefreshing(true);
    const scope: RefScope = refScope ?? {
      type: 'scenario',
      id: scenarioId,
      label: '',
    };
    // 仅当现有影响结果确实属于本次刷新 scope 时才静默保留；否则按非静默处理。
    const keepImpact = shouldKeepImpact(impact, scope);
    // 只在已有内容时静默：初始 loading / 错误态点刷新必须走正常 loading/error 路径，
    // 否则会永久停在 Skeleton 或把错误吞掉。
    void Promise.all([
      loadScenario(scenarioId, scenarioRefreshMode(Boolean(data)) === 'silent'),
      loadReferences(scope, keepImpact),
    ]).finally(() => {
      // 旧代刷新不得复位新代（或切场景后）的按钮态。
      if (my !== refreshSeq.current) return;
      refreshingRef.current = false;
      setRefreshing(false);
    });
  };

  const viewReferences = (type: 'object' | 'attribute', id: string, label: string) => {
    const scope: RefScope = { type, id, label };
    setRefScope(scope);
    setActiveTab('impact');
    void loadReferences(scope);
  };

  const backToScenario = () => {
    if (!scenarioId) return;
    setRefScope(null);
    void loadReferences({ type: 'scenario', id: scenarioId, label: '' });
  };

  const copyHash = (value: string) => {
    // 复制失败不影响查看，静默降级；http 非 localhost 下 copy-to-clipboard 会走 execCommand
    void copyToClipboard(value);
  };

  const objectNames = new Map((data?.objects ?? []).map((object) => [object.id, object.name]));
  const objectName = (id: string | null) => (id ? objectNames.get(id) ?? id : '—');

  const attributeColumns: ColumnsType<DgStandardAttribute> = [
    {
      title: t('dataGovernance.standards.attrName'),
      dataIndex: 'attrName',
      width: 180,
    },
    {
      title: t('dataGovernance.standards.attrType'),
      dataIndex: 'attrType',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.isPrimaryKey'),
      dataIndex: 'isPrimaryKey',
      width: 90,
      render: (value: boolean) => (
        <Tag color={value ? 'gold' : 'default'}>
          {t(value ? 'common.yes' : 'common.no')}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.standards.isRequired'),
      dataIndex: 'isRequired',
      width: 90,
      render: (value: boolean) => (
        <Tag color={value ? 'blue' : 'default'}>
          {t(value ? 'common.yes' : 'common.no')}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.standards.ontologyCode'),
      dataIndex: 'ontologyCode',
      width: 160,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.constraintsJson'),
      dataIndex: 'constraintsJson',
      width: 180,
      render: (value: Record<string, unknown>) =>
        Object.keys(value).length ? (
          <Collapse
            ghost
            size="small"
            items={[
              {
                key: 'json',
                label: t('dataGovernance.standards.constraintsJson'),
                children: (
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                    {JSON.stringify(value, null, 2)}
                  </pre>
                ),
              },
            ]}
          />
        ) : (
          '—'
        ),
    },
    {
      title: '',
      key: 'actions',
      width: 130,
      render: (_: unknown, attr) => (
        <Button
          size="small"
          onClick={() => viewReferences('attribute', attr.id, attr.attrName)}
        >
          {t('dataGovernance.standards.viewReferencedKbs')}
        </Button>
      ),
    },
  ];

  const relationColumns: ColumnsType<DgStandardRelation> = [
    { title: t('dataGovernance.standards.relations'), dataIndex: 'name', width: 180 },
    {
      title: t('dataGovernance.standards.relationType'),
      dataIndex: 'relationType',
      width: 130,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.standards.sourceObject'),
      dataIndex: 'sourceObjectId',
      width: 180,
      render: (value: string | null) => objectName(value),
    },
    {
      title: t('dataGovernance.standards.targetObject'),
      dataIndex: 'targetObjectId',
      width: 180,
      render: (value: string | null) => objectName(value),
    },
    {
      title: t('dataGovernance.standards.ontologyCode'),
      dataIndex: 'ontologyCode',
      width: 160,
      render: (value: string | null) => value || '—',
    },
  ];

  const constraintColumns: ColumnsType<DgStandardConstraint> = [
    { title: t('dataGovernance.standards.constraints'), dataIndex: 'name', width: 180 },
    {
      title: t('dataGovernance.standards.target'),
      dataIndex: 'targetLabel',
      width: 180,
      render: (value: string) => value || '—',
    },
    {
      title: t('dataGovernance.standards.constraintType'),
      dataIndex: 'constraintType',
      width: 130,
    },
    {
      title: t('dataGovernance.standards.expression'),
      dataIndex: 'expression',
      render: (value: string | null) => value || '—',
    },
  ];

  const impactColumns: ColumnsType<DgStandardReference> = [
    { title: t('dataGovernance.standards.kb'), dataIndex: 'kbName', width: 180 },
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

  const objectsContent =
    !data || data.objects.length === 0 ? (
      <Empty description={t('dataGovernance.standards.emptyObjects')} />
    ) : (
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {data.objects.map((object) => (
          <Collapse
            key={object.id}
            items={[
              {
                key: object.id,
                label: (
                  <Space size={[4, 4]} wrap>
                    <span style={{ fontWeight: 600 }}>{object.name}</span>
                    {object.status && <Tag>{object.status}</Tag>}
                    {object.ontologyCode && <Tag color="blue">{object.ontologyCode}</Tag>}
                  </Space>
                ),
                extra: (
                  <Button
                    size="small"
                    onClick={(event) => {
                      event.stopPropagation();
                      viewReferences('object', object.id, object.name);
                    }}
                  >
                    {t('dataGovernance.standards.viewReferencedKbs')}
                  </Button>
                ),
                children: (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    {object.description && (
                      <div className="yx-dg-muted">{object.description}</div>
                    )}
                    <Table<DgStandardAttribute>
                      rowKey="id"
                      size="small"
                      columns={attributeColumns}
                      dataSource={object.attributes}
                      pagination={false}
                      scroll={{ x: 1060 }}
                      locale={{
                        emptyText: (
                          <Empty
                            description={t('dataGovernance.standards.emptyAttributes')}
                          />
                        ),
                      }}
                    />
                  </Space>
                ),
              },
            ]}
          />
        ))}
      </Space>
    );

  const relationsContent =
    !data || data.relations.length === 0 ? (
      <Empty description={t('dataGovernance.standards.emptyRelations')} />
    ) : (
      <Table<DgStandardRelation>
        rowKey="id"
        size="small"
        columns={relationColumns}
        dataSource={data.relations}
        pagination={false}
        scroll={{ x: 830 }}
        locale={{
          emptyText: <Empty description={t('dataGovernance.standards.emptyRelations')} />,
        }}
      />
    );

  const constraintsContent = (
    <Table<DgStandardConstraint>
      rowKey="id"
      size="small"
      columns={constraintColumns}
      dataSource={data?.constraints ?? []}
      pagination={false}
      scroll={{ x: 690 }}
      locale={{
        emptyText: <Empty description={t('dataGovernance.standards.constraintsEmpty')} />,
      }}
    />
  );

  const impactContent = impactLoading ? (
    <Skeleton active paragraph={{ rows: 4 }} />
  ) : impactError ? (
    <Alert
      type="error"
      showIcon
      title={impactError}
      action={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => {
            if (!refScope && scenarioId) {
              void loadReferences({ type: 'scenario', id: scenarioId, label: '' });
            } else if (refScope) {
              void loadReferences(refScope);
            }
          }}
        >
          {t('dataGovernance.standards.retry')}
        </Button>
      }
    />
  ) : (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <span style={{ fontWeight: 600 }}>
          {refScope
            ? refScope.type === 'object'
              ? t('dataGovernance.standards.scopeObject', { name: refScope.label })
              : t('dataGovernance.standards.scopeAttribute', { name: refScope.label })
            : t('dataGovernance.standards.scopeScenario')}
        </span>
        {refScope && (
          <Button size="small" onClick={backToScenario}>
            {t('dataGovernance.standards.backToScenario')}
          </Button>
        )}
      </Space>
      {impact?.sourceIdCoverage === 'partial' && (
        <Alert type="warning" showIcon title={t('dataGovernance.standards.sourceIdNote')} />
      )}
      {impact?.truncated && (
        <Alert type="info" showIcon title={t('dataGovernance.standards.truncated')} />
      )}
      {!impact || impact.items.length === 0 ? (
        <Empty description={t('dataGovernance.standards.emptyImpact')} />
      ) : (
        <Table<DgStandardReference>
          rowKey="kbId"
          size="small"
          columns={impactColumns}
          dataSource={impact.items}
          pagination={false}
          scroll={{ x: 1000 }}
        />
      )}
    </Space>
  );

  // ── 发布版本历史 + diff ──────────────────────────────────────────

  const renderChangeValue = (value: unknown) => {
    if (value === null || value === undefined || value === '') return '—';
    return String(value);
  };

  const renderFieldChanges = (changes: DgFieldChange[]) => (
    <Table<DgFieldChange>
      rowKey="field"
      size="small"
      pagination={false}
      dataSource={changes}
      columns={[
        {
          title: t('dataGovernance.standards.versions.field'),
          dataIndex: 'field',
          width: 180,
        },
        {
          title: t('dataGovernance.standards.versions.from'),
          dataIndex: 'from',
          render: renderChangeValue,
        },
        {
          title: t('dataGovernance.standards.versions.to'),
          dataIndex: 'to',
          render: renderChangeValue,
        },
      ]}
    />
  );

  const renderEntityDiff = (group: DgEntityDiff) => {
    const hasAny =
      group.added.length + group.removed.length + group.changed.length > 0;
    if (!hasAny) return null;
    return (
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        {group.added.length > 0 && (
          <div>
            <Tag color="green">
              {t('dataGovernance.standards.versions.added')}
            </Tag>
            {group.added.map((item) => item.label).join('、')}
          </div>
        )}
        {group.removed.length > 0 && (
          <div>
            <Tag color="red">
              {t('dataGovernance.standards.versions.removed')}
            </Tag>
            {group.removed.map((item) => item.label).join('、')}
          </div>
        )}
        {group.changed.map((item) => (
          <div key={item.id}>
            <Tag color="gold">
              {t('dataGovernance.standards.versions.changed')}
            </Tag>
            <span style={{ fontWeight: 600 }}>{item.label}</span>
            {item.fields && item.fields.length > 0 && (
              <div style={{ marginTop: 4 }}>
                {renderFieldChanges(item.fields)}
              </div>
            )}
          </div>
        ))}
      </Space>
    );
  };

  const versionSelectOptions = versions.map((item) => ({
    value: item.version,
    label: `v${item.version}`,
  }));

  type EntityGroupKey = 'objects' | 'attributes' | 'relations' | 'constraints';

  const sumGroups: Array<{ key: EntityGroupKey; labelKey: string }> = [
    { key: 'objects', labelKey: 'dataGovernance.standards.objects' },
    { key: 'attributes', labelKey: 'dataGovernance.standards.attributes' },
    { key: 'relations', labelKey: 'dataGovernance.standards.relations' },
    { key: 'constraints', labelKey: 'dataGovernance.standards.constraints' },
  ];

  const diffContent = diff ? (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={[4, 4]} wrap>
        {diff.summary.scenario_changed > 0 && (
          <Tag color="gold">
            {t('dataGovernance.standards.versions.scenarioChanges')}
            {' · '}
            {diff.summary.scenario_changed}
          </Tag>
        )}
        {sumGroups.flatMap(({ key, labelKey }) => {
          const group = diff[key] as DgEntityDiff;
          return [
            group.added.length > 0 && (
              <Tag key={`${key}-added`} color="green">
                {t(labelKey)} {t('dataGovernance.standards.versions.added')}
                {' · '}
                {group.added.length}
              </Tag>
            ),
            group.removed.length > 0 && (
              <Tag key={`${key}-removed`} color="red">
                {t(labelKey)} {t('dataGovernance.standards.versions.removed')}
                {' · '}
                {group.removed.length}
              </Tag>
            ),
            group.changed.length > 0 && (
              <Tag key={`${key}-changed`} color="gold">
                {t(labelKey)} {t('dataGovernance.standards.versions.changed')}
                {' · '}
                {group.changed.length}
              </Tag>
            ),
          ].filter(Boolean);
        })}
        {Object.values(diff.summary).every((value) => value === 0) && (
          <span className="yx-dg-muted">
            {t('dataGovernance.standards.versions.noChanges')}
          </span>
        )}
      </Space>
      {diff.scenario.changed.length > 0 && (
        <div>
          <div className="yx-dg-section-title">
            {t('dataGovernance.standards.versions.scenarioChanges')}
          </div>
          {renderFieldChanges(diff.scenario.changed)}
        </div>
      )}
      {sumGroups.map(({ key, labelKey }) => {
        const block = renderEntityDiff(diff[key] as DgEntityDiff);
        if (!block) return null;
        return (
          <div key={key}>
            <div className="yx-dg-section-title">{t(labelKey)}</div>
            {block}
          </div>
        );
      })}
    </Space>
  ) : null;

  const versionsContent = versionsLoading ? (
    <Skeleton active paragraph={{ rows: 4 }} />
  ) : versionsError ? (
    <Alert
      type="error"
      showIcon
      title={versionsError}
      action={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => {
            if (scenarioId) void loadVersions(scenarioId);
          }}
        >
          {t('dataGovernance.standards.retry')}
        </Button>
      }
    />
  ) : versions.length === 0 ? (
    <Empty description={t('dataGovernance.standards.versions.empty')} />
  ) : (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Table<DgScenarioVersion>
        rowKey="id"
        size="small"
        pagination={false}
        dataSource={versions}
        columns={[
          {
            title: t('dataGovernance.standards.versions.version'),
            dataIndex: 'version',
            width: 90,
            render: (value: number) => `v${value}`,
          },
          {
            title: t('dataGovernance.standards.versions.publishedAt'),
            dataIndex: 'publishedAt',
            width: 180,
            render: (value: string | null) =>
              value ? formatDateTime(value) : '—',
          },
          {
            title: t('dataGovernance.standards.versions.publishedBy'),
            dataIndex: 'publishedBy',
            width: 140,
            render: (value: string | null) => value || '—',
          },
          {
            title: t('dataGovernance.standards.versions.hash'),
            dataIndex: 'structureHash',
            width: 180,
            render: (value: string | null) =>
              value ? <code title={value}>{value.slice(0, 12)}…</code> : '—',
          },
          {
            title: '',
            key: 'actions',
            width: 280,
            render: (_: unknown, row: DgScenarioVersion) => (
              <Space size={4}>
                <Button
                  size="small"
                  disabled={row.version <= 1}
                  onClick={() => {
                    if (!scenarioId) return;
                    setDiffFrom(row.version - 1);
                    setDiffTo(row.version);
                    void loadDiff(scenarioId, row.version - 1, row.version);
                  }}
                >
                  {t('dataGovernance.standards.versions.comparePrev')}
                </Button>
                {canRollback && (
                  <Popconfirm
                    title={t('dataGovernance.standards.versions.rollbackConfirm', {
                      version: row.version,
                    })}
                    okText={t('dataGovernance.standards.versions.rollback')}
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void handleRollback(row.version)}
                  >
                    <Button size="small" danger>
                      {t('dataGovernance.standards.versions.rollback')}
                    </Button>
                  </Popconfirm>
                )}
              </Space>
            ),
          },
        ]}
      />
      <Space size={8} wrap>
        <span>{t('dataGovernance.standards.versions.diffFrom')}</span>
        <Select
          style={{ width: 110 }}
          value={diffFrom ?? undefined}
          placeholder="—"
          options={versionSelectOptions}
          onChange={(value) => setDiffFrom(value)}
        />
        <span>{t('dataGovernance.standards.versions.diffTo')}</span>
        <Select
          style={{ width: 110 }}
          value={diffTo ?? undefined}
          placeholder="—"
          options={versionSelectOptions}
          onChange={(value) => setDiffTo(value)}
        />
        <Button
          type="primary"
          loading={diffLoading}
          disabled={diffFrom === null || diffTo === null}
          onClick={() => {
            if (scenarioId) void loadDiff(scenarioId, diffFrom, diffTo);
          }}
        >
          {t('dataGovernance.standards.versions.diffAction')}
        </Button>
      </Space>
      {diffError && <Alert type="error" showIcon title={diffError} />}
      {diffLoading ? <Skeleton active paragraph={{ rows: 4 }} /> : diffContent}
    </Space>
  );

  // ── 落标校验 ──────────────────────────────────────────────────────

  const complianceStatusColor = (
    status: DgKbComplianceItem['status'],
  ): string => {
    switch (status) {
      case 'current':
        return 'success';
      case 'outdated':
        return 'warning';
      case 'mismatch':
        return 'error';
      case 'not_published':
        return 'default';
      default:
        return 'default';
    }
  };

  const renderComplianceEntities = (
    title: string,
    list: DgComplianceEntity[],
  ) => {
    if (list.length === 0) return null;
    return (
      <div>
        <div className="yx-dg-section-title">{title}</div>
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          {list.map((item) => (
            <div key={`${title}-${item.object ?? ''}-${item.id}`}>
              <Tag>{item.label}</Tag>
              {item.object && item.object !== item.label && (
                <span className="yx-dg-muted" style={{ marginRight: 6 }}>
                  {item.object}
                </span>
              )}
              {item.fields?.map((field) => (
                <span key={field.field} style={{ marginRight: 8 }}>
                  {field.field}: {renderChangeValue(field.standard)} →{' '}
                  {renderChangeValue(field.compiled)}
                </span>
              ))}
            </div>
          ))}
        </Space>
      </div>
    );
  };

  const complianceDiffCount = (row: DgKbComplianceItem) =>
    row.missingObjects.length +
    row.extraObjects.length +
    row.missingAttributes.length +
    row.extraAttributes.length +
    row.changedAttributes.length;

  const complianceContent = complianceLoading ? (
    <Skeleton active paragraph={{ rows: 4 }} />
  ) : complianceError ? (
    <Alert
      type="error"
      showIcon
      title={complianceError}
      action={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => {
            if (scenarioId) void loadCompliance(scenarioId);
          }}
        >
          {t('dataGovernance.standards.retry')}
        </Button>
      }
    />
  ) : !compliance || compliance.items.length === 0 ? (
    <Empty description={t('dataGovernance.standards.compliance.empty')} />
  ) : (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={[4, 4]} wrap>
        <Tag>{t('dataGovernance.standards.compliance.total')} · {compliance.summary.total ?? 0}</Tag>
        {(['current', 'outdated', 'mismatch', 'no_schema', 'no_source_version', 'not_published'] as const).map(
          (status) =>
            (compliance.summary[status] ?? 0) > 0 && (
              <Tag key={status} color={complianceStatusColor(status)}>
                {t(`dataGovernance.standards.compliance.statuses.${status}`)} ·{' '}
                {compliance.summary[status]}
              </Tag>
            ),
        )}
        <span className="yx-dg-muted">
          {t('dataGovernance.standards.compliance.latestVersion')}:{' '}
          {compliance.latestPublishedVersion
            ? `v${compliance.latestPublishedVersion}`
            : t('dataGovernance.standards.compliance.notPublished')}
        </span>
      </Space>
      <Table<DgKbComplianceItem>
        rowKey="kbId"
        size="small"
        dataSource={compliance.items}
        pagination={false}
        scroll={{ x: 1000 }}
        expandable={{
          rowExpandable: (row) => complianceDiffCount(row) > 0,
          expandedRowRender: (row) => (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {renderComplianceEntities(
                t('dataGovernance.standards.compliance.missingObjects'),
                row.missingObjects,
              )}
              {renderComplianceEntities(
                t('dataGovernance.standards.compliance.extraObjects'),
                row.extraObjects,
              )}
              {renderComplianceEntities(
                t('dataGovernance.standards.compliance.missingAttributes'),
                row.missingAttributes,
              )}
              {renderComplianceEntities(
                t('dataGovernance.standards.compliance.extraAttributes'),
                row.extraAttributes,
              )}
              {renderComplianceEntities(
                t('dataGovernance.standards.compliance.changedAttributes'),
                row.changedAttributes,
              )}
            </Space>
          ),
        }}
        columns={[
          {
            title: t('dataGovernance.standards.compliance.kb'),
            dataIndex: 'kbName',
            width: 220,
            render: (value: string | null, row) => value || row.kbId,
          },
          {
            title: t('dataGovernance.standards.compliance.status'),
            dataIndex: 'status',
            width: 130,
            render: (status: DgKbComplianceItem['status']) => (
              <Tag color={complianceStatusColor(status)}>
                {t(`dataGovernance.standards.compliance.statuses.${status}`)}
              </Tag>
            ),
          },
          {
            title: t('dataGovernance.standards.compliance.version'),
            key: 'version',
            width: 150,
            render: (_: unknown, row) =>
              `${row.sourceVersion ?? '—'} / ${compliance.latestPublishedVersion ?? '—'}`,
          },
          {
            title: t('dataGovernance.standards.compliance.schemaVersion'),
            dataIndex: 'schemaVersion',
            width: 110,
            render: (value: number | null) => value ?? '—',
          },
          {
            title: t('dataGovernance.standards.compliance.syncStatus'),
            dataIndex: 'syncStatus',
            width: 110,
            render: (value: string | null) => value || '—',
          },
          {
            title: t('dataGovernance.standards.compliance.diffCount'),
            key: 'diff',
            width: 100,
            render: (_: unknown, row) => complianceDiffCount(row),
          },
          {
            title: t('dataGovernance.standards.compliance.docStats'),
            key: 'docStats',
            width: 220,
            render: (_: unknown, row) => <DocStatsCell stats={row.docStats} />,
          },
        ]}
      />
    </Space>
  );

  const tabItems = [
    { key: 'objects', label: t('dataGovernance.standards.objects'), children: objectsContent },
    {
      key: 'relations',
      label: t('dataGovernance.standards.relations'),
      children: relationsContent,
    },
    {
      key: 'constraints',
      label: t('dataGovernance.standards.constraints'),
      children: constraintsContent,
    },
    { key: 'impact', label: t('dataGovernance.standards.impact'), children: impactContent },
    {
      key: 'versions',
      label: t('dataGovernance.standards.versions.tab'),
      children: versionsContent,
    },
    {
      key: 'compliance',
      label: t('dataGovernance.standards.compliance.tab'),
      children: complianceContent,
    },
  ];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={960}
      destroyOnHidden
      title={data?.scenario.name || t('dataGovernance.standards.scenarios')}
      extra={
        <Tooltip title={t('dataGovernance.standards.refresh')}>
          <Button
            aria-label={t('dataGovernance.standards.refresh')}
            icon={<ReloadOutlined />}
            loading={refreshing || loading}
            disabled={refreshing || loading}
            onClick={refresh}
          />
        </Tooltip>
      }
    >
      {loading || (open && scenarioId && !data && !error) ? (
        <Skeleton active />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                if (scenarioId) void loadScenario(scenarioId);
              }}
            >
              {t('dataGovernance.standards.retry')}
            </Button>
          }
        />
      ) : !data ? (
        <Empty description={t('dataGovernance.standards.emptyScenarios')} />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {refreshError && (
            <Alert type="warning" showIcon title={refreshError} />
          )}
          <Descriptions size="small" column={2}>
            <Descriptions.Item label={t('dataGovernance.standards.domain')}>
              {domainName || data.scenario.domainId || '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.version')}>
              {data.scenario.version ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.publishedAt')}>
              {isUnpublished(data.scenario.publishedAt)
                ? t('dataGovernance.standards.unpublished')
                : formatDateTime(data.scenario.publishedAt)}
            </Descriptions.Item>
            <Descriptions.Item label={t('dataGovernance.standards.structureHash')}>
              {data.scenario.structureHash ? (
                <Space size={4}>
                  <code style={{ wordBreak: 'break-all' }}>
                    {data.scenario.structureHash}
                  </code>
                  <Button
                    size="small"
                    aria-label={t('dataGovernance.standards.copyStructureHash')}
                    icon={<CopyOutlined />}
                    onClick={() => {
                      if (data.scenario.structureHash) {
                        copyHash(data.scenario.structureHash);
                      }
                    }}
                  />
                </Space>
              ) : (
                '—'
              )}
            </Descriptions.Item>
          </Descriptions>
          <Alert type="info" showIcon title={t('dataGovernance.standards.publishedNote')} />
          <Tabs
            activeKey={activeTab}
            onChange={(key) => setActiveTab(key as TabKey)}
            items={tabItems}
          />
        </Space>
      )}
    </Drawer>
  );
}
