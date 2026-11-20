/**
 * 抽象 schema 编辑器（知识库级）。
 *
 * 数据流：GET abstract-schema(v2) → toDocument → editorStore → 画布；
 * 保存 = toServerBody → PUT（草稿 proposed / 确认 active），乐观锁按 schema_version。
 * active 时只读，提供「转为草稿」；编辑期间（proposed）投影与检索概念扩展不生效
 * ——按钮 tooltip 里说清（设计决策）。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Alert, Button, Checkbox, Drawer, Empty, Input, Modal, Progress, Space, Spin, Table, Tag, Tooltip, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PartitionOutlined, ReloadOutlined, SaveOutlined, CheckCircleOutlined, EditOutlined, UploadOutlined, ThunderboltOutlined, HistoryOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import {
  generateAbstractSchema,
  getAbstractSchema,
  getAbstractSchemaHistoryDetail,
  listAbstractSchemaHistory,
  restoreAbstractSchemaHistory,
  saveAbstractSchema,
  type AbstractMetric,
  type AbstractSchema,
  type AbstractSchemaHistoryDetail,
  type AbstractSchemaHistoryItem,
} from '@/api/abstractSchema';
import {
  getLandingRun,
  startLandingRun,
  type LandingRunMode,
} from '@/api/landingAgent';
import type { AttributeType } from '../ontologyCanvas/domain/index.js';
import { ModelCanvas } from '../ontologyCanvas/components/ModelCanvas.js';
import { PropertiesPanel } from '../ontologyCanvas/components/PropertiesPanel.js';
import { checkForExport } from '../ontologyCanvas/domain/index.js';
import { layoutAll } from '../ontologyCanvas/layout.js';
import { useEditorStore } from '../ontologyCanvas/store/editorStore.js';
import { toDocument, toServerBody } from './adapters.js';
import { LandingSection } from './LandingSection.js';
import MetricsPanel from './MetricsPanel.js';
import { ImportTboxModal } from './ImportTboxModal.js';
// 画布基础样式与 React Flow 样式住在 ../ontologyCanvas/styles.scss（`@use '@xyflow/react/dist/style.css'`）。
// 编辑器要自带：本组件有两条挂载路（路由 `-` 走 OntologyCanvasPage，页签由 DomainKnowledgeDetail 直接
// 引用本文件）——只靠前者 import 的话，页签那条路的 ontology-canvas-* / react-flow 样式一条都加载不到。
import '../ontologyCanvas/styles.scss';
import './styles.scss';

const { Text } = Typography;

function formatHistoryTime(value: string | null, locale?: string): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale || undefined);
}

interface Props {
  kbId: string;
  canWrite: boolean;
}

export function AbstractSchemaEditorPage({ kbId, canWrite }: Props): ReactNode {
  const { t, i18n } = useTranslation();
  const [loading, setLoading] = useState(true);
  /** 生成概念候选（与「抽象 Schema」页签同一动作）：空态与有数据时都可触发 */
  const [generating, setGenerating] = useState(false);
  /** 后台 run 的进度（仅生成中非空）：阶段 + 概念计数 + 落点/未决数。 */
  const [runProgress, setRunProgress] = useState<{
    done: number; total: number; phase: string; subPhase: string;
    landed: number; unresolved: number;
  } | null>(null);
  /** 进度弹层：提交即打开；可「后台运行」收起，点行内进度重新打开。 */
  const [progressOpen, setProgressOpen] = useState(false);
  /** 轮询定时器句柄：重生成与卸载都必须清掉，否则旧 run 的轮询会写进新状态。 */
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 生成代次：卸载/重新生成会自增，用来丢弃在途的 start / poll 响应（不能只清定时器）。 */
  const runTokenRef = useRef(0);
  /** 生成前的小弹窗：可选填业务词表（客户会怎么说），生成时挂到对应实体上 */
  const [generateOpen, setGenerateOpen] = useState(false);
  /** 有草稿时默认只补落点；勾上 = 清空草稿全量重来。 */
  const [restartAll, setRestartAll] = useState(false);
  const [aliasHints, setAliasHints] = useState('');
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [schema, setSchema] = useState<AbstractSchema | null>(null);
  const [relayoutTick, setRelayoutTick] = useState(0);
  // 指标独立于画布文档：编辑面在 Drawer 里（MetricsPanel），随页面保存一并提交。
  const [metrics, setMetrics] = useState<AbstractMetric[]>([]);
  const [metricsDirty, setMetricsDirty] = useState(false);
  const [metricsOpen, setMetricsOpen] = useState(false);
  // 保存历史 Drawer：列表只在打开时拉，预览详情单独拉（列表不带 schema）。
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyItems, setHistoryItems] = useState<AbstractSchemaHistoryItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string>();
  const [previewDetail, setPreviewDetail] = useState<AbstractSchemaHistoryDetail | null>(null);
  /** 正在拉详情的快照 id（`previewDetail` 只在返回后才有，不能拿它判断行内 loading）。 */
  const [previewingId, setPreviewingId] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);

  const store = useEditorStore();

  // 左右两栏的展开/收起：
  // - 左栏（实体列表）由人手动控制，默认展开；
  // - 右栏（属性面板）**跟随选中**：选中实体自动展开，取消选中自动收起；
  //   人也可以手动收起，下次重新选中时再自动展开（可预期，不用记上次手动的状态）。
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(false);
  const hasSelection = Boolean(store.selection);
  useEffect(() => { setRightOpen(hasSelection); }, [hasSelection]);
  const { workingDocument: document } = store;
  const readOnly = !canWrite || schema?.status === 'active';

  const load = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    store.reset();
    try {
      const data = await getAbstractSchema(kbId);
      setSchema(data);
      setMetrics(data?.schema.metrics ?? []);
      setMetricsDirty(false);
      if (data) {
        const doc = toDocument(kbId, data.schema);
        doc.editor.nodePositions = layoutAll(doc);
        store.initialize(doc);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // ⚠️ `store` / `t` **刻意不进依赖**，与 OntologyCanvasPage 同款：
    // `useEditorStore()` 不带 selector 时每次 set 都换 state 对象身份，而 load 自己
    // 就要写 store（reset/initialize）——把 store 放进依赖会立刻自激（load 换身份 →
    // effect 重跑 → 再 GET → 再写 store …），挂载即死循环、每轮还打一条 GET。
    // zustand 的 action 引用稳定，闭包里捕获的那份调用起来与最新一份等价。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId]);

  useEffect(() => { void load(); }, [load]);

  // ── 保存历史（2026-10-27）：列表 / 预览 / 一键还原 ─────────────────────

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError(undefined);
    try {
      const data = await listAbstractSchemaHistory(kbId);
      setHistoryItems(data.items);
    } catch (e) {
      setHistoryItems([]);
      setHistoryError(e instanceof Error ? e.message : String(e));
    } finally {
      setHistoryLoading(false);
    }
  }, [kbId]);

  const openHistory = useCallback(() => {
    setHistoryOpen(true);
    setPreviewDetail(null);
    void loadHistory();
  }, [loadHistory]);

  const previewHistory = useCallback(async (row: AbstractSchemaHistoryItem) => {
    setPreviewingId(row.id);
    try {
      setPreviewDetail(await getAbstractSchemaHistoryDetail(row.id, kbId));
    } catch (e: any) {
      if (e?.status === 404 || e?.bizCode === 1002) {
        message.error(t('abstractEditor.history.snapshotExpired'));
        await loadHistory();
      } else {
        message.error(e instanceof Error ? e.message : t('abstractEditor.history.previewFailed'));
      }
    } finally {
      setPreviewingId(null);
    }
  }, [kbId, loadHistory, t]);

  const doRestore = useCallback(async (row: AbstractSchemaHistoryItem) => {
    setRestoring(true);
    try {
      // 乐观锁 = **当前行**版本（不是快照版本）：编辑页可能开着很久，
      // 不带当前版本就是拿旧内容静默覆盖别人刚保存的新内容。
      await restoreAbstractSchemaHistory(row.id, kbId, schema?.schemaVersion ?? 0);
      message.success(t('abstractEditor.history.restoreSuccess', { version: row.schemaVersion }));
      // 关闭预览；成功即刷新页面数据（新版本上屏）与历史列表（新增 restore 快照）。
      setPreviewDetail(null);
      await load();
      await loadHistory();
    } catch (e: any) {
      if (e?.status === 409 || e?.bizCode === 1003) {
        message.error(t('abstractEditor.history.restoreConflict'));
        // 冲突说明本地版本已过期：刷新列表让用户看到最新快照；
        // 当前 schema 不自动重拉，避免静默丢弃画布上未保存的编辑。
        await loadHistory();
      } else if (e?.status === 404 || e?.bizCode === 1002) {
        message.error(t('abstractEditor.history.snapshotExpired'));
        await loadHistory();
      } else {
        message.error(e?.message || t('abstractEditor.history.restoreFailed'));
      }
    } finally {
      setRestoring(false);
    }
  }, [kbId, load, loadHistory, schema, t]);

  const confirmRestore = useCallback((row: AbstractSchemaHistoryItem) => {
    // 还原成功后本页会 `load()`（store.reset → 画布回服务端内容）——
    // 有未保存编辑时必须先讲清楚，不能等刷新完再让用户发现丢了。
    const dirty = useEditorStore.getState().dirty || metricsDirty;
    const currentStatus = schema?.status;
    const statusChanged = Boolean(currentStatus) && row.status !== currentStatus;
    const statusLabel = (value?: 'proposed' | 'active') => (
      value === 'active'
        ? t('abstractSchema.statusActive')
        : value === 'proposed'
          ? t('abstractSchema.statusProposed')
          : '—'
    );
    Modal.confirm({
      title: t('abstractEditor.history.restoreTitle'),
      content: (
        <div>
          <div>
            {t('abstractEditor.history.restoreConfirm', {
              version: row.schemaVersion,
              current: schema?.schemaVersion ?? 0,
            })}
          </div>
          <div style={{ marginTop: 8 }}>
            {t('abstractEditor.history.restoreSnapshotStatus', {
              status: statusLabel(row.status),
              current: statusLabel(currentStatus),
            })}
          </div>
          {statusChanged ? (
            <div style={{ marginTop: 8, color: '#faad14' }}>
              {row.status === 'proposed' && currentStatus === 'active'
                ? t('abstractEditor.history.restoreDeactivateWarning')
                : t('abstractEditor.history.restoreStatusChangeWarning', {
                    status: statusLabel(row.status),
                    current: statusLabel(currentStatus),
                  })}
            </div>
          ) : null}
          {dirty ? (
            <div style={{ marginTop: 8, color: '#ff4d4f' }}>
              {t('abstractEditor.history.restoreDirtyWarning')}
            </div>
          ) : null}
        </div>
      ),
      okText: t('abstractEditor.history.restoreOk'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: () => doRestore(row),
    });
  }, [doRestore, metricsDirty, schema, t]);

  const historyColumns: ColumnsType<AbstractSchemaHistoryItem> = [
    {
      title: t('abstractEditor.history.version'),
      dataIndex: 'schemaVersion',
      width: 78,
      render: (value: number) => <Tag color="blue">v{value}</Tag>,
    },
    {
      title: t('abstractEditor.history.status'),
      dataIndex: 'status',
      width: 78,
      render: (value: AbstractSchemaHistoryItem['status']) => (
        <Tag color={value === 'active' ? 'green' : 'orange'}>
          {value === 'active' ? t('abstractSchema.statusActive') : t('abstractSchema.statusProposed')}
        </Tag>
      ),
    },
    {
      title: t('abstractEditor.history.source'),
      dataIndex: 'source',
      width: 68,
      render: (value: AbstractSchemaHistoryItem['source']) => (
        <Tag>{value === 'restore' ? t('abstractEditor.history.sourceRestore') : t('abstractEditor.history.sourceManual')}</Tag>
      ),
    },
    {
      title: t('abstractEditor.history.savedBy'),
      dataIndex: 'savedBy',
      render: (value: string | null) => value || '—',
    },
    {
      title: t('abstractEditor.history.createdAt'),
      dataIndex: 'createdAt',
      width: 150,
      render: (value: string | null) => formatHistoryTime(value, i18n?.language),
    },
    {
      title: t('abstractEditor.history.actions'),
      key: 'actions',
      width: 152,
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button size="small" loading={previewingId === row.id}
            onClick={() => void previewHistory(row)}>
            {t('abstractEditor.history.preview')}
          </Button>
          <Button size="small" danger disabled={!canWrite || restoring}
            onClick={() => confirmRestore(row)}>
            {t('abstractEditor.history.restore')}
          </Button>
        </Space>
      ),
    },
  ];

  const issues = useMemo(() => (document ? checkForExport(document) : []), [document]);
  const mustFix = issues.filter((item) => item.level === 'mustFix');

  const save = useCallback(async (status: 'proposed' | 'active') => {
    if (!document || !schema) return;
    setSaving(true);
    try {
      const body = await saveAbstractSchema({
        knowledgeBaseId: kbId,
        schema: { ...toServerBody(document), metrics },
        status,
        expectedSchemaVersion: schema.schemaVersion,
      });
      setSchema(body);
      store.markSaved(body.schemaVersion, body.editedAt ?? '');
      setMetricsDirty(false);
      message.success(status === 'active' ? t('abstractSchema.confirmed') : t('abstractSchema.saved'));
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      if (text.includes('版本') || text.includes('已被更新') || text.includes('CONFLICT')) {
        message.error(t('abstractSchema.conflict'));
      } else {
        message.error(text);
      }
    } finally {
      setSaving(false);
    }
    // `metrics` 必进依赖：只改指标时 `document` 不变，闭包里那份会停在上一次渲染
    // ——点保存提交的就是旧指标（静默丢编辑），与 load() 注释里的同一类坑。
  }, [document, kbId, metrics, schema, store, t]);

  const stopPolling = useCallback(() => {
    if (pollRef.current != null) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // 卸载兜底：切页/关抽屉时后台 run 照常跑完，但本页不能再轮询（会对已卸载组件 setState）。
  // 自增代次是为了让「startLandingRun 还没回来就卸载」的在途响应也作废（此时定时器还没建）。
  useEffect(() => () => {
    runTokenRef.current += 1;
    stopPolling();
  }, [stopPolling]);

  /** 旧同步生成路径（开关关闭的兜底 / 1001 回退）。保持原有 409 二次确认。 */
  const runLegacyGenerate = useCallback(async (force: boolean, hints: string): Promise<void> => {
    try {
      // 默认走「复用文档本体 + 自动落点」：只做结构化聚类会得到泛化属性名
      // （「发电量」同时挂在汇总/明细实体上）且没有业务别名/落点 —— 检索词面
      // 匹配不到、或被同库多实体歧义挡住，数据补充整库跳过（2026-10-20 两次重建
      // demo 库都踩到）。文档本体 + 自动落点让 schema 直接带中文叫法与落点。
      const data = await generateAbstractSchema(kbId, force, undefined, {
        seed: 'document',
        autoLandings: true,
        aliasHints: hints,
      });
      message.success(
        t('abstractSchema.generated', { count: data?.schema?.entities?.length ?? 0 }),
      );
      await load();
    } catch (e: any) {
      // 已有已确认的抽象 schema：显式确认后才 force 覆盖（与抽象 Schema 页签同口径）
      if (!force && (e?.status === 409 || e?.bizCode === 1003)) {
        if (window.confirm(t('abstractSchema.overwriteConfirm'))) {
          await runLegacyGenerate(true, hints);
        }
        return;
      }
      message.error(e?.message || t('abstractSchema.generateFailed'));
    }
  }, [kbId, load, t]);

  /**
   * 生成入口：优先走后台 run（`landings` 只补落点、`full` 全量重来），每 2s 轮询；
   * 开关关闭 / 参数无效（1001）回退到旧同步 generate，保证关闭态部署仍可用。
   */
  const handleGenerateConcepts = useCallback(async (
    runMode: LandingRunMode,
    hints = '',
    force = false,
  ): Promise<void> => {
    // 只读（active / 无写权）时不许生成：按钮已禁用，这里是最后一道守卫。
    if (readOnly) return;
    // 重新生成 = 旧 run 的轮询必须立刻停（它只会把过期进度写进新一次生成）。
    stopPolling();
    const token = ++runTokenRef.current;
    setGenerating(true);
    setRunProgress(null);
    // 提交即弹层：用户第一眼就看见「正在做什么」，而不是等第一次轮询。
    setProgressOpen(true);
    let polling = false;
    try {
      let started = await startLandingRun(kbId, runMode, {
        aliasHints: hints,
        force,
      }).catch(async (e: any) => {
        // 已有已确认（active）的抽象 schema：没带 force 时后端回 1003。
        // 与旧同步生成同口径，先让用户确认覆盖，再带 force 重试一次。
        if (!force && (e?.status === 409 || e?.bizCode === 1003)) {
          if (!window.confirm(t('abstractSchema.overwriteConfirm'))) return null;
          return startLandingRun(kbId, runMode, { aliasHints: hints, force: true });
        }
        throw e;
      });
      if (started == null) return;
      // 在途期间被重新生成/卸载：这次响应作废，别建定时器。
      if (token !== runTokenRef.current) return;
      const runId = started.runId;
      polling = true;
      // 这次 run 已经落单，「全部重来」不再粘住下一次。
      setRestartAll(false);
      let stopped = false;
      // 自调度 setTimeout 链：只有当前响应落地后才排下一拍。
      // 固定 interval 在慢请求下会叠加并发轮询，同一终态可能触发两次 load()/toast。
      function schedule(): void {
        if (stopped || token !== runTokenRef.current) return;
        pollRef.current = setTimeout(() => { void tick(); }, 2000);
      }
      async function tick(): Promise<void> {
        pollRef.current = null;
        try {
          const run = await getLandingRun(kbId, runId);
          if (stopped || token !== runTokenRef.current) return;
          const { conceptsDone, conceptsTotal, subPhase, landed, unresolved } = run.progress;
          setRunProgress({
            done: conceptsDone,
            total: conceptsTotal,
            phase: run.phase,
            subPhase,
            landed,
            unresolved,
          });
          if (run.status === 'succeeded') {
            stopped = true;
            stopPolling();
            setRunProgress(null);
            setProgressOpen(false);
            setGenerating(false);
            message.success(t('abstractSchema.landingAgent.succeeded'));
            await load();
          } else if (run.status === 'failed') {
            stopped = true;
            stopPolling();
            setRunProgress(null);
            setProgressOpen(false);
            setGenerating(false);
            message.error(run.error || t('abstractSchema.generateFailed'));
          } else {
            schedule();
          }
        } catch (e: any) {
          if (stopped || token !== runTokenRef.current) return;
          // run 不见了（1002）是终态；其它错误（网络抖动）留到下一拍重试。
          if (e?.status === 404 || e?.bizCode === 1002) {
            stopped = true;
            stopPolling();
            setRunProgress(null);
            setProgressOpen(false);
            setGenerating(false);
            message.error(e?.message || t('abstractSchema.generateFailed'));
          } else {
            schedule();
          }
        }
      }
      // 立即拉一次：queued/running 的第一帧进度不必等满 2s。
      void tick();
    } catch (e: any) {
      if (token !== runTokenRef.current) return;
      if (e?.bizCode === 1001) {
        // 开关关闭 / 参数无效：后台入口不可用时旧同步生成仍然可用。
        message.info(t('abstractSchema.landingAgent.fallbackHint'));
        await runLegacyGenerate(false, hints);
        return;
      }
      if (e?.status === 429 || e?.bizCode === 3007) {
        message.error(t('abstractSchema.landingAgent.busy'));
        return;
      }
      message.error(e?.message || t('abstractSchema.generateFailed'));
    } finally {
      // 成功建单后由轮询收口；这里只负责「没进轮询」的分支复位按钮态。
      // 代次变了说明已被重新生成/卸载接管，不能再改当前状态。
      if (!polling && token === runTokenRef.current) {
        setGenerating(false);
        setProgressOpen(false);
      }
    }
  }, [kbId, load, readOnly, runLegacyGenerate, stopPolling, t]);

  const addEntity = useCallback(() => {
    // 只读守卫：UI 已处处禁用，这里是最后一道（抽象 schema 的 active = 全只读契约）
    if (readOnly || !document) return;
    const used = new Set(document.model.entities.map((item) => item.name));
    let index = document.model.entities.length + 1;
    while (used.has(`Entity_${index}`)) index += 1;
    const name = `Entity_${index}`;
    store.execute(
      { type: 'entity.add', entity: { id: `e:${name}`, name, aliases: [], attributes: [], landings: [] }, position: { x: 80, y: 80 } },
      { kind: 'entity', id: `e:${name}` },
    );
  }, [document, readOnly, store]);

  /** 画布与属性面板**共用同一个回调**（两处传的是它，不是两份实现）——否则同一动作
   *  在两处会漂移出不同命名规则。 */
  const addAttribute = useCallback((entityId: string, type: AttributeType) => {
    if (readOnly || !document) return;
    const entity = document.model.entities.find((item) => item.id === entityId);
    if (!entity) return;
    const used = new Set(entity.attributes.map((item) => item.name));
    let index = entity.attributes.length + 1;
    while (used.has(`attr_${index}`)) index += 1;
    const name = `attr_${index}`;
    store.execute(
      { type: 'attribute.add', entityId, attribute: { id: `a:${entity.name}.${name}`, name, type, required: false, primaryKey: false } },
      { kind: 'entity', id: entityId },
    );
  }, [document, readOnly, store]);

  // 判「有草稿」看**实体数**而不是 document 非空：空 schema 也会建出 workingDocument，
  // 拿它跑 landings 就是零概念走一遍、upsert 回原样的空 schema 并报成功。
  const hasDraft = Boolean(document && document.model.entities.length > 0);
  /** 本次提交模式：有内容的草稿默认只补落点；「全部重来」或空草稿走全量。 */
  const draftMode: LandingRunMode = hasDraft && !restartAll ? 'landings' : 'full';
  // queued 阶段后端 phase 为空串；别把 `...phase.` 这种残缺 key 显示出来。
  const phaseLabel = (phase: string) => (phase ? t(`abstractSchema.landingAgent.phase.${phase}`) : '');
  /** 当前阶段：主阶段 +（落点阶段的）子相位，如「判定落点 · 判定落点」去重。 */
  const currentPhaseText = runProgress
    ? Array.from(new Set([
        phaseLabel(runProgress.phase),
        runProgress.subPhase ? phaseLabel(runProgress.subPhase) : '',
      ].filter(Boolean))).join(' · ')
    : '';
  const runPercent = runProgress && runProgress.total > 0
    ? Math.min(100, Math.round((runProgress.done / runProgress.total) * 100))
    : null;
  /** 行内进度：生成中常驻，点一下重新打开弹层（弹层可「后台运行」收起）。 */
  const runProgressNode = generating ? (
    <button type="button" className="ae-run-progress" onClick={() => setProgressOpen(true)}>
      {t('abstractSchema.landingAgent.inline', {
        phase: currentPhaseText || t('abstractSchema.landingAgent.queued'),
        done: runProgress?.done ?? 0,
        total: runProgress?.total ?? 0,
      })}
    </button>
  ) : null;
  /**
   * 执行进度弹层：生成在服务端后台跑，这里只负责「现在在做什么」。
   * 不阻断操作：可收起成行内进度，完成后自动关闭并刷新草稿。
   */
  const runProgressModal = (
    <Modal
      open={generating && progressOpen}
      title={t('abstractSchema.landingAgent.runningTitle')}
      onCancel={() => setProgressOpen(false)}
      maskClosable={false}
      footer={(
        <Button type="primary" onClick={() => setProgressOpen(false)}>
          {t('abstractSchema.landingAgent.runInBackground')}
        </Button>
      )}
      width={520}
    >
      <div className="ae-run-modal">
        <Space align="center" size="small" className="ae-run-modal-head">
          <Spin size="small" />
          <Text strong>{currentPhaseText || t('abstractSchema.landingAgent.queued')}</Text>
        </Space>
        {runPercent !== null ? (
          <Progress percent={runPercent} status="active" showInfo={false} />
        ) : null}
        {runProgress && runProgress.total > 0 ? (
          <Text type="secondary">
            {t('abstractSchema.landingAgent.counters', {
              done: runProgress.done,
              total: runProgress.total,
              landed: runProgress.landed,
              unresolved: runProgress.unresolved,
            })}
          </Text>
        ) : null}
        <Text type="secondary" className="ae-run-modal-hint">
          {t('abstractSchema.landingAgent.runHint')}
        </Text>
      </div>
    </Modal>
  );

  /**
   * 生成前的小弹窗：业务词表可选。词表语法是「别名1,别名2 => 实体名或物理表名」，
   * 生成后由后端 `apply_alias_hints` 确定性挂到实体上（不调模型）——客户怎么叫，
   * 检索就怎么命中；留空则只用文档本体自动生成。
   */
  const generateModal = (
    <Modal
      title={t('abstractSchema.generate')}
      open={generateOpen}
      onCancel={() => { setGenerateOpen(false); setRestartAll(false); }}
      okText={t('abstractSchema.aliasHints.confirm')}
      cancelText={t('common.cancel')}
      confirmLoading={generating}
      onOk={() => {
        setGenerateOpen(false);
        void handleGenerateConcepts(draftMode, aliasHints.trim());
      }}
      width={640}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Text type="secondary">
          {t('abstractSchema.landingAgent.modeLabel', {
            mode: draftMode === 'full'
              ? t('abstractSchema.landingAgent.modeFull')
              : t('abstractSchema.landingAgent.modeLandings'),
          })}
        </Text>
        <Checkbox
          checked={restartAll}
          disabled={!hasDraft}
          onChange={(e) => setRestartAll(e.target.checked)}
        >
          {t('abstractSchema.landingAgent.restart')}
        </Checkbox>
        <Text type="secondary">{t('abstractSchema.aliasHints.hint')}</Text>
        {/* 落点模式不重造概念/别名（那是「全部重来」的职责），词表在本模式下
            没有作用域：禁用输入，避免用户填了却完全不生效。 */}
        <Input.TextArea
          rows={6}
          value={aliasHints}
          disabled={draftMode === 'landings'}
          onChange={(e) => setAliasHints(e.target.value)}
          placeholder={t('abstractSchema.aliasHints.placeholder')}
        />
      </Space>
    </Modal>
  );

  if (loading) return <div className="ontology-canvas-loading"><Spin /></div>;
  if (error) return <Alert type="error" showIcon message={t('abstractSchema.loadFailed')} description={error} />;
  if (!schema) {
    return (
      <>
        <Empty description={t('abstractEditor.emptySchema')}>
          <Space direction="vertical" align="center" size="small">
            <Text type="secondary">{t('abstractEditor.emptyHint')}</Text>
            {/* 空态也要有「下一步」：与概念对齐一致，按钮常驻而不是只留一句提示 */}
            <Button type="primary" icon={<ThunderboltOutlined />} loading={generating} disabled={readOnly}
              onClick={() => setGenerateOpen(true)}>
              {t('abstractSchema.generate')}
            </Button>
            <Button icon={<UploadOutlined />} disabled={!canWrite}
              onClick={() => setImportOpen(true)}>
              {t('abstractEditor.importTbox')}
            </Button>
            {runProgressNode}
          </Space>
        </Empty>
        <ImportTboxModal
          kbId={kbId}
          expectedSchemaVersion={0}   /* 首次导入：0 = 「期望没有行」（upsert 的 INSERT 分支） */
          open={importOpen}
          onClose={() => setImportOpen(false)}
          onImported={() => void load()}
          /* 导入成功后的重新上屏会盖掉本地未保存编辑（`getState()` 现读 +
             `metricsDirty` 本地 state）。 */
          confirmDirty={() => !(useEditorStore.getState().dirty || metricsDirty)
            || window.confirm(t('abstractEditor.importDirtyConfirm'))}
        />
        {generateModal}
        {runProgressModal}
      </>
    );
  }
  if (!document) return null;

  return (
    <div className="ontology-canvas-page ae-page">
      <header className="ontology-canvas-toolbar">
        <Space wrap>
          <strong>{t('abstractEditor.title')}</strong>
          <Tag color={schema.status === 'active' ? 'green' : 'orange'}>
            {schema.status === 'active' ? t('abstractSchema.statusActive') : t('abstractSchema.statusProposed')}
          </Tag>
          <Text type="secondary">{t('abstractSchema.versionTag', { v: schema.schemaVersion })}</Text>
          {schema.staleCount > 0 ? <Tag color="warning">{t('abstractSchema.staleCount', { count: schema.staleCount })}</Tag> : null}
          {store.dirty || metricsDirty ? <Tag color="blue">{t('abstractSchema.unsaved')}</Tag> : null}
        </Space>
        <Space>
          {/* 只读（active）下禁用：landings-only 不能改已确认基线，full 也不该
              在无提示下覆盖 —— 与 save/确认按钮同一道 readOnly 闸。 */}
          <Button icon={<ThunderboltOutlined />} loading={generating} disabled={readOnly}
            onClick={() => setGenerateOpen(true)}>
            {t('abstractSchema.generate')}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>{t('common.refresh')}</Button>
          <Button icon={<HistoryOutlined />} onClick={openHistory}>
            {t('abstractEditor.history.button')}
          </Button>
          <Button icon={<UploadOutlined />} disabled={!canWrite}
            onClick={() => setImportOpen(true)}>
            {t('abstractEditor.importTbox')}
          </Button>
          {/* 打开面板在只读（active）下照常可用：只闸编辑，不闸查看（面板内字段随 readOnly 禁用）。 */}
          <Button onClick={() => setMetricsOpen(true)}>
            {t('abstractEditor.metricsButton', { count: metrics.length })}
          </Button>
          {schema.status === 'active' ? (
            // 「转为草稿」会把**当前工作文档**整份以 proposed 落库。之所以不存在
            // 「把 active 期间的手滑编辑一起带过去」：active 下画布与属性面板**全只读**
            // （readOnly 传给 ModelCanvas / PropertiesPanel / LandingSection，重新布局
            // 也一并禁用）——不可能产生未保存编辑，PUT 上去的就是读到的那一份。
            <Tooltip title={t('abstractEditor.toDraftHint')}>
              <Button icon={<EditOutlined />} disabled={!canWrite} loading={saving} onClick={() => void save('proposed')}>
                {t('abstractEditor.toDraft')}
              </Button>
            </Tooltip>
          ) : (
            <>
              <Button icon={<SaveOutlined />} disabled={readOnly || !(store.dirty || metricsDirty)} loading={saving} onClick={() => void save('proposed')}>
                {t('abstractSchema.saveDraft')}
              </Button>
              <Button type="primary" icon={<CheckCircleOutlined />} disabled={readOnly || mustFix.length > 0} loading={saving} onClick={() => void save('active')}>
                {t('abstractSchema.confirm')}
              </Button>
            </>
          )}
          <Tooltip title={t('ontologyCanvas.relayoutHint')}>
            {/* 重新布局会写 `layout.apply`（置 dirty）——只读下必须一起闸掉：
                否则「全只读 ⇒ 无未保存编辑」的不变量当场破功 */}
            <Button icon={<PartitionOutlined />} disabled={readOnly || !document.model.entities.length} onClick={() => {
              store.execute({ type: 'layout.apply', positions: layoutAll(document) });
              setRelayoutTick((tick) => tick + 1);
            }}>{t('ontologyCanvas.relayout')}</Button>
          </Tooltip>
        </Space>
      </header>

      {runProgressNode ? <div className="ae-run-progress-row">{runProgressNode}</div> : null}

      {mustFix.length ? (
        <Alert type="error" showIcon className="ontology-canvas-issues"
          message={t('ontologyCanvas.mustFix', { n: mustFix.length })}
          description={mustFix.slice(0, 5).map((item) => `${item.code} · ${item.target?.id ?? '-'}`).join('；')} />
      ) : null}

      <div
        className={[
          'ontology-canvas-body',
          leftOpen ? '' : 'is-left-collapsed',
          rightOpen && hasSelection ? '' : 'is-right-collapsed',
        ].filter(Boolean).join(' ')}
      >
        <button
          type="button"
          className="canvas-panel-toggle canvas-panel-toggle--left"
          title={leftOpen ? t('abstractEditor.collapseList') : t('abstractEditor.expandList')}
          aria-label={leftOpen ? t('abstractEditor.collapseList') : t('abstractEditor.expandList')}
          onClick={() => setLeftOpen((open) => !open)}
        >
          {leftOpen ? <LeftOutlined /> : <RightOutlined />}
        </button>
        <aside className="ontology-canvas-list">
          {document.model.entities.length === 0 ? (
            <p className="ontology-canvas-list-empty">{t('editor.emptyCanvas')}</p>
          ) : document.model.entities.map((entity) => {
            const selected = store.selection?.kind === 'entity' && store.selection.id === entity.id;
            return (
              <button key={entity.id} type="button"
                className={`ontology-canvas-list-item ${selected ? 'is-selected' : ''}`}
                onClick={() => store.select({ kind: 'entity', id: entity.id })}>
                <span className="name">{entity.displayName || entity.name}</span>
                <span className="code">{entity.name}</span>
                <span className="count">{(entity.landings ?? []).filter((l) => l.included).length}</span>
              </button>
            );
          })}
        </aside>

        <ModelCanvas
          document={document}
          selection={store.selection}
          execute={store.execute}
          select={store.select}
          addEntity={addEntity}
          addAttribute={addAttribute}
          relayoutTick={relayoutTick}
          readOnly={readOnly}
        />

        <PropertiesPanel
          document={document}
          selection={store.selection}
          execute={store.execute}
          addAttribute={addAttribute}
          readOnly={readOnly}
          // 字段落点编辑要拉某源编译 schema 的列清单（getCompiledSchema(kbId, dsId)）。
          kbId={kbId}
          // 本字段集在抽象态下：关系三键、属性显示名称、来源块都渲染（三键保存链路
          // 一期已通；另两者为仅抽象态往返的字段）。编译画布不传 fieldSet。
          fieldSet="abstract"
          entityExtras={(entity) => (
            <LandingSection kbId={kbId} entity={entity} entityNames={document.model.entities.map((e) => e.name)} execute={store.execute} readOnly={readOnly} />
          )}
          relationExtras={(relation) => (
            <div className="ae-backings">
              <p className="ae-backings-title">{t('abstractEditor.backingsTitle')}</p>
              {(relation.backings ?? []).length === 0 ? (
                <p className="ae-landings-empty">{t('abstractEditor.noBacking')}</p>
              ) : (relation.backings ?? []).map((b) => (
                <div key={`${b.dataSourceId}:${b.relationName}`} className="ae-landing">
                  <Tag className="ae-landing-src">{b.dataSourceId}</Tag>
                  <code>{b.relationName}</code>
                  {b.sourceColumn || b.targetColumn ? (
                    <Text type="secondary">{`${b.sourceColumn ?? '?'} = ${b.targetColumn ?? '?'}`}</Text>
                  ) : null}
                  {b.stale ? <Tag color="warning">{t('abstractEditor.staleTag')}</Tag> : null}
                </div>
              ))}
            </div>
          )}
        />
        <button
          type="button"
          className="canvas-panel-toggle canvas-panel-toggle--right"
          title={rightOpen && hasSelection
            ? t('abstractEditor.collapsePanel')
            : t('abstractEditor.expandPanel')}
          aria-label={rightOpen && hasSelection
            ? t('abstractEditor.collapsePanel')
            : t('abstractEditor.expandPanel')}
          disabled={!hasSelection}
          onClick={() => setRightOpen((open) => !open)}
        >
          {rightOpen && hasSelection ? <RightOutlined /> : <LeftOutlined />}
        </button>
      </div>

      {/* 保存仍走页面 toolbar 那两个按钮——Drawer footer 只给「完成」关闭
          （指标编辑独立成 state，关掉面板不丢：`metricsDirty` 已并入页面的未保存判定）。 */}
      <Drawer title={t('abstractEditor.metricsDrawerTitle')} width={720}
        open={metricsOpen} onClose={() => setMetricsOpen(false)}
        footer={<Button type="primary" onClick={() => setMetricsOpen(false)}>{t('abstractEditor.metricsDone')}</Button>}>
        <MetricsPanel kbId={kbId} value={metrics} disabled={readOnly}
          onChange={(next) => { setMetrics(next); setMetricsDirty(true); }} />
      </Drawer>

      {/* 保存历史：每次公开保存（含本页保存/确认、TBox 导入、还原）都留一条快照。
          列表不带 schema，预览才拉详情；还原走公开保存语义，会产生一条新的 restore 快照。 */}
      <Drawer
        title={t('abstractEditor.history.title')}
        width={640}
        open={historyOpen}
        onClose={() => { setHistoryOpen(false); setPreviewDetail(null); }}
        extra={
          <Button size="small" icon={<ReloadOutlined />} loading={historyLoading}
            data-testid="history-refresh" onClick={() => void loadHistory()}>
            {t('common.refresh')}
          </Button>
        }
      >
        {historyError ? (
          <Alert
            type="error"
            showIcon
            message={t('abstractEditor.history.loadFailed')}
            description={historyError}
            action={
              <Button size="small" onClick={() => void loadHistory()}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : (
          <Table<AbstractSchemaHistoryItem>
            size="small"
            rowKey="id"
            loading={historyLoading}
            columns={historyColumns}
            dataSource={historyItems}
            pagination={false}
            locale={{ emptyText: t('abstractEditor.history.empty') }}
          />
        )}
      </Drawer>

      <Modal
        title={previewDetail
          ? t('abstractEditor.history.previewTitle', { version: previewDetail.schemaVersion })
          : ''}
        open={!!previewDetail}
        onCancel={() => setPreviewDetail(null)}
        width={520}
        footer={[
          <Button key="close" onClick={() => setPreviewDetail(null)}>{t('common.close')}</Button>,
          <Button key="restore" type="primary" danger disabled={!canWrite || restoring}
            onClick={() => previewDetail && confirmRestore(previewDetail)}>
            {t('abstractEditor.history.restore')}
          </Button>,
        ]}
      >
        {previewDetail ? (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Text>
              {t('abstractEditor.history.previewCounts', {
                entities: previewDetail.schema.entities.length,
                relations: previewDetail.schema.relations.length,
                metrics: (previewDetail.schema.metrics ?? []).length,
              })}
            </Text>
            <div>
              <Text type="secondary">{t('abstractEditor.history.previewEntities')}</Text>
              <div style={{ marginTop: 8 }}>
                {previewDetail.schema.entities.length === 0 ? (
                  <Text type="secondary">{t('abstractEditor.history.previewEmpty')}</Text>
                ) : previewDetail.schema.entities.slice(0, 6).map((entity) => (
                  <Tag key={entity.name}>{entity.displayName || entity.name}</Tag>
                ))}
              </div>
            </div>
          </Space>
        ) : null}
      </Modal>

      <ImportTboxModal
        kbId={kbId}
        expectedSchemaVersion={schema.schemaVersion}
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={() => void load()}
        /* 导入成功后的重新上屏会盖掉本地未保存编辑（`getState()` 现读 +
           `metricsDirty` 本地 state）。 */
        confirmDirty={() => !(useEditorStore.getState().dirty || metricsDirty)
          || window.confirm(t('abstractEditor.importDirtyConfirm'))}
      />
      {generateModal}
      {runProgressModal}
    </div>
  );
}

export default AbstractSchemaEditorPage;
