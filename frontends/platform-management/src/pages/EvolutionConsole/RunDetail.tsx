import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Checkbox, Collapse, Descriptions, Empty, Input, Modal, Select,
  Space, Spin, Steps, Table, Tabs, Tag, Tooltip, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircleOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import {
  confirmAdopt, getRunBundle, getRunLog, listVersions, previewAdopt, promoteAdhoc, rejectRun,
  rollbackRun,
  type AdoptPreview, type AdoptResult, type AdhocTruth, type AdhocVerdict, type Artifact,
  type CandidateAdhocCase,
  type CandidateItem, type CliResult, type PatchEntry, type PromoteAdhocConflict,
  type RollbackResult, type RunBundle, type SemanticVersion, type SmokeResult,
} from '../../api/evolution';
import { STAGE_ORDER, isRollbackDone, parseStage, stageIndex, stageLabelKey, statusTagColor } from './constants';
import EntryAdjustModal from './EntryAdjustModal';
import AdhocTruthModal from './components/AdhocTruthModal';
import AdhocVerifyModal from './components/AdhocVerifyModal';
import BenchCard from './components/BenchCard';
import CandidateCard from './components/CandidateCard';
import GateCard from './components/GateCard';
import MarkdownLite from './components/MarkdownLite';
import SmokeCard, { outcomeColor } from './components/SmokeCard';
import SqlPre from './components/SqlPre';
import VersionList, { isArchivedVersion, isLegacyVersion } from './components/VersionList';
import { createSequenceGuard } from './sequenceGuard';
import { resolveAdoptingStart } from './adoptingTimer';
import { adoptRequestKey, useAdoptRequestId } from './useAdoptRequestId';
import { humanizeSkipReason, relationEdgeOf, skipItemLabel } from './skipReasons';

const POLL_MS = 5_000;
/** adopting 轮询退避：前 5 分钟 5s（尽快看到父本/候选推进），之后 15s。 */
const POLL_SLOW_MS = 15_000;
const ADOPT_SLOW_AFTER_MS = 5 * 60_000;
/** 超过 30 分钟仍在 adopting：显示 warning，但**不停止**轮询。 */
const ADOPT_WARN_AFTER_MS = 30 * 60_000;

/** 零条生效：evolve.py 是 `raise SystemExit("零条生效…")` → 串在 stderr、rc=1。两流都要看。 */
const isNoopResult = (r: Pick<CliResult, 'stdout_tail' | 'stderr_tail'>) =>
  `${r.stdout_tail ?? ''}${r.stderr_tail ?? ''}`.includes('零条生效');

/** constraint 条目（P1-4）：`{op: add|update|disable|enable, ...}`；其它 kind 的条目没有 `op`。 */
const CONSTRAINT_OPS = ['add', 'update', 'disable', 'enable'] as const;
type ConstraintEntryOp = typeof CONSTRAINT_OPS[number];
const constraintOpOf = (e: PatchEntry): ConstraintEntryOp | null => {
  const op = typeof e.op === 'string' ? e.op : null;
  return op !== null && (CONSTRAINT_OPS as readonly string[]).includes(op)
    ? (op as ConstraintEntryOp)
    : null;
};

/** relation remove 条目（P1 收尾）：`op === "remove"`；add / 发现器条目没有 op。
 * constraint / mapping 只会有 add/update/disable/enable，不会误入此分支。 */
const relationOpOf = (e: PatchEntry): boolean => e.op === 'remove';

/** tool_schema 条目（Tool-Schema 线）：只会有 update / disable / enable，按工具 name 定位。 */
const TOOL_SCHEMA_OPS = ['update', 'disable', 'enable'] as const;
type ToolSchemaOp = typeof TOOL_SCHEMA_OPS[number];
const toolSchemaOpOf = (e: PatchEntry): ToolSchemaOp | null => {
  const op = typeof e.op === 'string' ? e.op : null;
  return op !== null && (TOOL_SCHEMA_OPS as readonly string[]).includes(op)
    ? (op as ToolSchemaOp)
    : null;
};

/** mapping 的复合身份键：`target_code|ds|entity|column`（None → 空串），
 * 与后端 `engine.mapping_key` 逐字同口径。 */
const MAPPING_SLOT_KEYS = ['role', 'filter', 'aggregation', 'grain'] as const;
type MappingSlotKey = typeof MAPPING_SLOT_KEYS[number];

const mappingKeyOf = (e: PatchEntry) => [
  e.target_code, e.data_source_id, e.entity_name, e.attribute_name,
].map((value) => (value == null ? '' : String(value).trim())).join('|');

/** synonym 条目的一组词（trim 后去空，保序）—— 与后端 `entry_key` 的归一化同口径。 */
const synonymTermsOf = (e: { terms?: unknown }): string[] =>
  (Array.isArray(e.terms) ? e.terms : [])
    .map((term) => String(term ?? '').trim())
    .filter(Boolean);

/** synonym 条目键：`词1/词2:canonical`（与后端 `entry_key(e, "synonym")` 逐字对齐）。 */
const synonymKeyOf = (e: PatchEntry) =>
  `${synonymTermsOf(e).join('/')}:${String(e.canonical ?? '').trim()}`;

/** synonym 条目展示：`词1 / 词2 → canonical`。 */
const synonymLabelOf = (e: { terms?: unknown; canonical?: unknown }) =>
  `${synonymTermsOf(e).join(' / ')} → ${String(e.canonical ?? '').trim()}`;

/** 与后端 `engine.entry_key` **逐字同口径**（人审 picks 要能对上，别加本地花样）：
 * alias → `Concept:别名`；relation（有 source+target）→ 边名 `name`（可能为空串）；
 * constraint（add/update/disable/enable）→ `name`（add 的 name 由后端生成，
 * planned 里已带；用 target_code 会与 `_pick_entries` 对不上 → 1001）；
 * mapping → `target_code|ds|entity|column`（同一抽象属性的多个落点不能互相串选）；
 * synonym → `词1/词2:canonical`（同批 term 不重复；canonical 进键让「同词换名」可区分）；
 * 其它 → `target_code`。 */
const entryKeyOf = (e: PatchEntry, kind?: string | null) => {
  if (kind === 'mapping') return mappingKeyOf(e);
  if (kind === 'synonym') return synonymKeyOf(e);
  if (e.concept !== undefined) return `${e.concept}:${e.alias}`;
  if (e.op) return String(e.name ?? e.target_code ?? '');
  if ('source' in e && 'target' in e) return String(e.name ?? '');
  return String(e.target_code ?? '');
};

/** 条目展示名（勾选行左侧的 code）：relation 用两端而不是内部边名；
 * mapping 用物理 `entity.column`（target_code 另外展示）；synonym 用
 * `词1 / 词2 → canonical`；constraint add 展示 target_code（与 query_rule 同形），
 * 其它 constraint op 用 name。 */
const entryLabelOf = (e: PatchEntry, kind?: string | null) => {
  if (kind === 'mapping') {
    return [e.entity_name, e.attribute_name].filter(Boolean).join('.');
  }
  if (kind === 'synonym') return synonymLabelOf(e);
  return relationEdgeOf(e)
    ?? (e.op === 'add' ? String(e.target_code ?? e.name ?? '') : entryKeyOf(e, kind));
};

/** mapping 的槽变化：`before`（旧值，可能缺键）与 `after`/`changed`（新值）。
 * planned 期带 `changed`，apply 后带 `after`；`null`/空串 = 显式清空。 */
const mappingSlotPairs = (
  e: PatchEntry,
): { slot: MappingSlotKey; before?: string; after?: string }[] => {
  const before = e.before ?? {};
  const changed = e.changed ?? {};
  const after = e.after ?? {};
  const pairs: { slot: MappingSlotKey; before?: string; after?: string }[] = [];
  for (const slot of MAPPING_SLOT_KEYS) {
    const inChanged = slot in changed;
    const inAfter = slot in after;
    const inEntry = e[slot] !== undefined;
    if (!inChanged && !inAfter && !inEntry) continue;
    const oldValue = before[slot];
    const newValue = inChanged ? changed[slot] : inAfter ? after[slot] : e[slot];
    pairs.push({
      slot,
      before: oldValue == null ? undefined : String(oldValue),
      after: newValue == null ? undefined : String(newValue),
    });
  }
  return pairs;
};

/** 预览 planned 行：alias → `Concept:别名`；relation 的 `added[]` 带两端
 * （`{name, source, source_column, target, target_column}`）→ 展示边；constraint →
 * `op name[: 新表达式]`；mapping → `op target_code[: 槽=新值,…]`；synonym →
 * `op 词1 / 词2 → canonical`；其余回退到 name / target_code。 */
const plannedLabel = (p: Record<string, unknown>, kind?: string | null) => {
  if (kind === 'mapping') {
    const head = [String(p.op ?? ''), String(p.target_code ?? '')].filter(Boolean).join(' ');
    const changed = (p.changed ?? null) as Record<string, unknown> | null;
    const after = (p.after ?? null) as Record<string, unknown> | null;
    const source = changed ?? after ?? p;
    const slots = MAPPING_SLOT_KEYS
      .filter((slot) => source[slot] !== undefined && source[slot] !== null && source[slot] !== '')
      .map((slot) => `${slot}=${String(source[slot])}`);
    return slots.length ? `${head}: ${slots.join(', ')}` : head;
  }
  if (kind === 'synonym') {
    const op = typeof p.op === 'string' && p.op ? `${p.op} ` : '';
    return `${op}${synonymLabelOf(p)}`;
  }
  if (p.concept !== undefined) return `${p.concept}:${p.alias}`;
  const edge = relationEdgeOf(p);
  if (edge !== null) return edge;
  if (typeof p.op === 'string') {
    const name = String(p.name ?? p.target_code ?? '');
    // tool_schema 的可读产物是 description（没有 expression）。
    if (kind === 'tool_schema' && typeof p.description === 'string' && p.description) {
      return `${p.op} ${name}: ${p.description}`;
    }
    return p.expression ? `${p.op} ${name}: ${String(p.expression)}` : `${p.op} ${name}`;
  }
  return String(p.name ?? p.target_code ?? JSON.stringify(p));
};

/** RunDetail：概览 / 候选 / 版本三页签。
 * 概览 = run.json + 门结论 + smoke + 报告 + 条目人审（预览/采纳/否决/回滚）+ 日志 + 产物。 */
export default function RunDetail() {
  const { runId = '' } = useParams();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [bundle, setBundle] = useState<RunBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [adjustTarget, setAdjustTarget] = useState<PatchEntry | null>(null);
  const [preview, setPreview] = useState<AdoptPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const [adoptResult, setAdoptResult] = useState<AdoptResult | null>(null);
  const [overrideReason, setOverrideReason] = useState('');
  const [adhocTruths, setAdhocTruths] = useState<Record<string, AdhocTruth>>({});
  // alias 批的人工验真结论（票据绑定 picks；勾选一变即全部作废）。
  const [adhocVerdicts, setAdhocVerdicts] = useState<Record<string, AdhocVerdict>>({});
  const [adhocOpen, setAdhocOpen] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promoteChecked, setPromoteChecked] = useState<string[]>([]);
  const [promoting, setPromoting] = useState(false);
  const [promoteConflicts, setPromoteConflicts] = useState<PromoteAdhocConflict[]>([]);
  const [rejectNote, setRejectNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [logLines, setLogLines] = useState<string[] | null>(null);
  const [activeTab, setActiveTab] = useState('overview');
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [rollbackTarget, setRollbackTarget] = useState<string | undefined>(undefined);
  const [rollbackReason, setRollbackReason] = useState('');
  const [rollbackAck, setRollbackAck] = useState(false);
  const [rollbackResult, setRollbackResult] = useState<RollbackResult | null>(null);
  const [chainVersions, setChainVersions] = useState<SemanticVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const initRef = useRef<string | null>(null);
  // 幂等键按「采纳意图」管理：勾选/override/truth 变化时作废旧 id（C4-2）。
  const adoptRequest = useAdoptRequestId();
  // 版本链请求代际守卫：快速切 run 时旧响应不得覆盖新链（C4-2）。
  const versionsGuard = useRef(createSequenceGuard()).current;

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getRunBundle(runId);
      setBundle(res);
      const entries = Array.isArray(res.patch?.entries) ? res.patch.entries : [];
      const keys = entries.map((entry) => entryKeyOf(entry, res.run.kind));
      const fresh = initRef.current !== runId;
      if (fresh) initRef.current = runId;   // 本 run 首次载入：全选；之后：只保留仍存在的勾选（用户清空=保持清空）
      setSelected((prev) => (fresh ? keys : prev.filter((k) => keys.includes(k))));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [runId, t]);

  useEffect(() => {
    load();
  }, [load]);

  // 切换 run：上一个 run 的交互态（预览/采纳结果/truth/回滚确认/日志）全部作废，
  // 否则新 run 会短暂显示旧结果，甚至把旧 truth 带进新采纳。
  useEffect(() => {
    setSelected([]);
    setAdjustTarget(null);
    setPreview(null);
    setPreviewOpen(false);
    setAdoptResult(null);
    setOverrideReason('');
    setAdhocTruths({});
    setAdhocVerdicts({});
    setAdhocOpen(false);
    setPromoteOpen(false);
    setPromoteChecked([]);
    setPromoting(false);
    setPromoteConflicts([]);
    setRejectNote('');
    setRollbackOpen(false);
    setRollbackTarget(undefined);
    setRollbackReason('');
    setRollbackAck(false);
    setRollbackResult(null);
    setLogOpen(false);
    setLogLines(null);
    adoptRequest.reset();
  }, [runId, adoptRequest]);

  const runStatus = typeof bundle?.run.status === 'string' ? bundle.run.status : null;
  // adopting 也是「在飞」：后台正在跑完整语料基准，详情必须继续轮询。
  const runAdopting = runStatus === 'adopting';
  const isRunning = (runStatus === 'running' || runAdopting) && !bundle?.stale;

  // 进入 adopting 的时刻（轮询退避与 30 分钟告警都按它算）。
  // 优先用后端 `run.bench_started_at`：硬刷新后不能把 5 分钟快速窗口与
  // 30 分钟告警的计时重新归零。但 pending 阶段该字段可能是上一次尝试留下的
  // （重试复用同一 run 行），只有父本已跑（parent_ran 及以后）才可信。
  const adoptingSinceRef = useRef<number | null>(null);
  const adoptingRunRef = useRef<string | null>(null);
  const [adoptingLong, setAdoptingLong] = useState(false);
  const bundleRunId = typeof bundle?.run.run_id === 'string' ? bundle.run.run_id : null;
  const benchStatus = typeof bundle?.run.bench_status === 'string'
    ? bundle.run.bench_status
    : null;
  const benchStartedAtRaw = bundle?.run.bench_started_at;
  const benchStartedAt = typeof benchStartedAtRaw === 'string'
    ? Date.parse(benchStartedAtRaw)
    : Number.NaN;
  useEffect(() => {
    // 页面支持不卸载直接切 run：上一个 run 的计时与告警必须在这里作废。
    const switchedRun = adoptingRunRef.current !== runId;
    adoptingRunRef.current = runId;
    if (switchedRun || !runAdopting) {
      adoptingSinceRef.current = null;
      setAdoptingLong(false);
    }
    if (!runAdopting || adoptingSinceRef.current !== null) return;
    // 切 run 后 bundle 可能还停在上一个 run：等新 bundle 到了再初始化，
    // 否则会把上一个 run 的 bench_started_at 当成新 run 的计时起点。
    if (bundleRunId !== null && bundleRunId !== runId) return;
    adoptingSinceRef.current = resolveAdoptingStart(benchStatus, benchStartedAt, Date.now());
  }, [runId, runAdopting, bundleRunId, benchStatus, benchStartedAt]);

  useEffect(() => {
    if (!isRunning) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    // 动态间隔：每轮轮询后再决定下一轮用 5s 还是 15s（不能用固定 setInterval）。
    const schedule = () => {
      if (cancelled) return;
      let delay = POLL_MS;
      const startedAt = adoptingSinceRef.current;
      if (runAdopting && startedAt !== null) {
        const elapsed = Date.now() - startedAt;
        delay = elapsed > ADOPT_SLOW_AFTER_MS ? POLL_SLOW_MS : POLL_MS;
        if (elapsed > ADOPT_WARN_AFTER_MS) setAdoptingLong(true);
      }
      timer = window.setTimeout(() => {
        if (cancelled) return;
        void load().finally(schedule);
      }, delay);
    };
    schedule();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [isRunning, runAdopting, load]);

  // 版本链：`bundle.versions` 只有本 run 相关版本（来源 run ∪ 采纳 ∪ 父版本），
  // 看不到祖先与 archived。整条链按 run.kind 推导 scope/ds 后走 `/versions` 查询。
  const runKb = typeof bundle?.run.kb === 'string' ? bundle.run.kb : null;
  const runDs = typeof bundle?.run.ds === 'string' ? bundle.run.ds : null;
  const runKind = typeof bundle?.run.kind === 'string' ? bundle.run.kind : null;
  // relation / constraint 与 query_rule 共用 compiled 版本链（scope=compiled，见 P1-3 R1 / P1-4 R5）；
  // mapping / tool_schema 与 alias 同链（KB 级 abstract，见 P1-5 / Tool-Schema 线）；
  // synonym 是**独立**的 KB 级版本链（scope=synonym，不与 abstract 互斥）。
  const chainScope = runKind === 'alias' || runKind === 'mapping' || runKind === 'tool_schema'
    ? 'abstract'
    : runKind === 'synonym'
      ? 'synonym'
      : runKind === 'query_rule' || runKind === 'relation' || runKind === 'constraint'
        ? 'compiled'
        : undefined;
  const chainDs = chainScope === 'compiled' ? (runDs ?? undefined) : undefined;

  // alias 的验真结论绑定本次 picks：`selected` 不只在用户勾选时变化 ——
  // `load()` 刷新/采纳失败重载会按服务端条目集合裁剪它。只挂在 onChange 上会
  // 让 stale verdicts 残留（确认按钮仍可用，却拿旧 picks 的票据去确认 ⇒ 服务端拒）。
  // 内容真变才清空；首次载入用 prev=null 跳过（此时本来也没有判定）。
  const selectedKey = [...selected].sort().join('\n');
  const prevSelectedKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (prevSelectedKeyRef.current === null) {
      prevSelectedKeyRef.current = selectedKey;
      return;
    }
    if (prevSelectedKeyRef.current === selectedKey) return;
    prevSelectedKeyRef.current = selectedKey;
    if (runKind === 'alias' || runKind === 'synonym') {
      setAdhocVerdicts({});
      setAdhocOpen(false);
    }
  }, [selectedKey, runKind]);

  const loadVersions = useCallback(async () => {
    const seq = versionsGuard.begin();
    if (!runKb || !chainScope) {
      // 提前返回也必须清错误：否则上一个 run 的「加载失败」会挂在新 run 上。
      setChainVersions([]);
      setVersionsError(null);
      setVersionsLoading(false);
      return;
    }
    setVersionsLoading(true);
    setVersionsError(null);
    try {
      const res = await listVersions({
        kb: runKb,
        scope: chainScope,
        ds: chainDs,
        include_archived: true,
        limit: 200,
      });
      if (!versionsGuard.isLatest(seq)) return;   // 旧 run 的响应：丢弃
      setChainVersions(Array.isArray(res.items) ? res.items : []);
    } catch (e: unknown) {
      if (!versionsGuard.isLatest(seq)) return;
      // 拉取失败不白屏：版本页签与回滚候选回退到 bundle.versions。
      setVersionsError(e instanceof Error ? e.message : t('common.loadFailed'));
      setChainVersions([]);
    } finally {
      if (versionsGuard.isLatest(seq)) setVersionsLoading(false);
    }
  }, [runKb, chainScope, chainDs, t, versionsGuard]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  const bundleVersions: SemanticVersion[] = bundle?.versions ?? [];
  // 整条链（含 archived），按 created_at 倒序；接口失败/为空时回退到本 run 相关版本。
  const versions: SemanticVersion[] = (chainVersions.length ? chainVersions : bundleVersions)
    .slice()
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
  const parentVersionId = (() => {
    const value = bundle?.run.parent_version_id;
    return typeof value === 'string' && value ? value : null;
  })();

  const openLog = async () => {
    setLogOpen(true);
    if (logLines !== null && !isRunning) return;
    try {
      const res = await getRunLog(runId);
      setLogLines(res.exists ? res.lines : []);
    } catch {
      setLogLines([]);
    }
  };

  const openRollback = (targetId?: string) => {
    setRollbackTarget(targetId ?? parentVersionId ?? undefined);
    setRollbackReason('');
    setRollbackAck(false);
    setRollbackResult(null);
    setRollbackOpen(true);
  };

  const doPreview = async () => {
    if (!selected.length) {
      message.warning(t('evolutionConsole.previewNeedPick'));
      return;
    }
    setPreviewing(true);
    try {
      const res = await previewAdopt(runId, { picks: selected });
      setPreview(res);
      // 有未补 truth 的 ad-hoc：先收 control/expect，收集完再进确认弹窗。
      if ((res.adhoc_cases?.length ?? 0) > 0) setAdhocOpen(true);
      else setPreviewOpen(true);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setPreviewing(false);
    }
  };

  const submitAdhocTruths = (truths: AdhocTruth[]) => {
    const next: Record<string, AdhocTruth> = {};
    for (const truth of truths) next[truth.case_id] = truth;
    setAdhocTruths(next);
    setAdhocOpen(false);
    setPreviewOpen(true);
  };

  // alias 批走验真卡：结论带证据票据（picks 变化即失效），失败 verdict 只有理由。
  const submitAdhocVerdicts = (verdicts: AdhocVerdict[]) => {
    const next: Record<string, AdhocVerdict> = {};
    for (const verdict of verdicts) next[verdict.case_id] = verdict;
    setAdhocVerdicts(next);
    setAdhocOpen(false);
    setPreviewOpen(true);
  };

  const adhocCases = preview?.adhoc_cases ?? [];
  // alias / synonym 走「验真」（后端只对这两个 kind 提供 adhoc-evidence），
  // 其它 kind 走「补 truth」：文案分叉，避免验真用户看到补 truth。
  const verifyRun = runKind === 'alias' || runKind === 'synonym';
  const adhocCopy = verifyRun
    ? {
      cases: t('evolutionConsole.previewAdhocCasesVerify', { n: adhocCases.length }),
      missing: t('evolutionConsole.previewAdhocNeedVerify'),
      ready: t('evolutionConsole.previewAdhocReadyVerify'),
      fill: t('evolutionConsole.previewFillVerify'),
    }
    : {
      cases: t('evolutionConsole.previewAdhocCases', { n: adhocCases.length }),
      missing: t('evolutionConsole.previewAdhocNeedTruth'),
      ready: t('evolutionConsole.previewAdhocReady'),
      fill: t('evolutionConsole.previewFillAdhoc'),
    };
  const adhocMissing = adhocCases.filter((item) => (
    verifyRun ? !adhocVerdicts[item.case_id] : !adhocTruths[item.case_id]
  ));
  // 人工判定「不通过」= 明确否决：后端 409，前端更不该先发一轮请求。
  const adhocFailed = verifyRun
    ? adhocCases.filter((item) => adhocVerdicts[item.case_id]?.verdict === 'fail')
    : [];
  const overrideMissing = Boolean(preview?.override_required) && !overrideReason.trim();
  const adoptBlocked = Boolean(preview?.adhoc_veto)
    || overrideMissing || adhocMissing.length > 0 || adhocFailed.length > 0;
  // 幂等键作用域：勾选 + override 理由 + 本批已填的 ad-hoc 结论
  // （只算当前预览的 case，避免上一轮残留让同一次意图换新 id）。
  // alias / synonym 用 verdict 的 case_id + evidence_id（票据变化也是不同意图），
  // 其它 kind 用 truth。
  const adhocKeyIds = verifyRun
    ? adhocCases
      .map((item) => adhocVerdicts[item.case_id])
      .filter(Boolean)
      .map((verdict) => `${verdict.case_id}:${verdict.evidence_id ?? ''}`)
    : adhocCases
      .map((item) => item.case_id)
      .filter((caseId) => Boolean(adhocTruths[caseId]));
  const adoptKey = adoptRequestKey(selected, overrideReason, adhocKeyIds);

  const doConfirmAdopt = async () => {
    if (preview?.adhoc_veto) {
      message.error(t('evolutionConsole.previewAdhocVeto'));
      return;
    }
    if (overrideMissing) {
      message.warning(t('evolutionConsole.previewOverrideRequired'));
      return;
    }
    if (adhocFailed.length) {
      // 人工判「不通过」是硬否决：不往服务端跑一趟（后端也会 409）。
      message.error(t('evolutionConsole.previewAdhocFailVeto'));
      return;
    }
    if (adhocMissing.length) {
      message.warning(adhocCopy.missing);
      setAdhocOpen(true);
      return;
    }
    // 同一次采纳意图（key 未变）的重试复用；意图变了则自动换新 id。
    const requestId = adoptRequest.ensure(adoptKey);
    setAdopting(true);
    try {
      const res = await confirmAdopt(runId, {
        picks: selected,
        request_id: requestId,
        override_reason: overrideReason.trim() || undefined,
        // alias / synonym 批与其它 kind 互斥：前两者只送人工验真结论，其余只送
        // control truth。alias / synonym **总是**带 `adhoc_verdicts`（哪怕空数组）：
        // picks 排除了全部待验真 case 时，空数组明确表示「本次无需验真」；整键缺席
        // 会让后端回落到旧 truths 覆盖校验，拿全量 unverified 把采纳 1001 卡死（Critical 1）。
        ...(verifyRun
          ? { adhoc_verdicts: adhocCases.map((item) => adhocVerdicts[item.case_id]) }
          : (adhocCases.length
            ? { adhoc_truths: adhocCases.map((item) => adhocTruths[item.case_id]) }
            : {})),
      });
      // 202 受理：后台仍在跑基准，本请求还没收口。**不要** reset 幂等键——
      // 任何重试必须复用同一个 request_id（换新 id 会撞 409）。
      const accepted = res.accepted === true;
      setAdoptResult(res);
      setPreviewOpen(false);
      await load();
      await loadVersions();
      if (accepted) {
        message.success(t('evolutionConsole.adoptAccepted'));
        return;
      }
      // 只有终态采纳或明确的失败态才作废幂等键；超时/未知保持复用。
      const terminalFailure = !res.ok && !res.timeout && res.status !== 'adopting';
      if (res.adopted || terminalFailure) adoptRequest.reset();
      if (res.adopted) {
        setOverrideReason('');
        setAdhocTruths({});
        setAdhocVerdicts({});
        message.success(t('evolutionConsole.adoptOk'));
      } else if (res.timeout) message.error(t('evolutionConsole.adoptTimeout'));
      else if (isNoopResult(res)) message.info(t('evolutionConsole.adoptNoop'));
      else message.error(t('evolutionConsole.adoptFailed', { rc: res.rc }));
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setAdopting(false);
    }
  };

  const doReject = async () => {
    if (!rejectNote.trim()) {
      message.warning(t('evolutionConsole.rejectNeedNote'));
      return;
    }
    setBusy(true);
    try {
      const res = await rejectRun(runId, rejectNote.trim());
      if (!res.ok) {
        message.error(
          t('evolutionConsole.rejectFailed', {
            rc: res.rc,
            tail: (res.stderr_tail || res.stdout_tail || '-').slice(-200),
          }),
        );
      } else {
        message.success(t('evolutionConsole.rejectDone'));
      }
      await load();
      await loadVersions();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setBusy(false);
    }
  };

  const selectedRollbackVersion = versions.find((v) => v.version_id === rollbackTarget);
  const rollbackRisky = Boolean(
    selectedRollbackVersion
    && (isLegacyVersion(selectedRollbackVersion) || isArchivedVersion(selectedRollbackVersion)),
  );

  const doRollback = async () => {
    if (rollbackRisky && !rollbackAck) {
      message.warning(t('evolutionConsole.rollbackNeedAck'));
      return;
    }
    setBusy(true);
    try {
      const res = await rollbackRun(runId, {
        to_version_id: rollbackTarget || undefined,
        reason: rollbackReason.trim(),
      });
      setRollbackResult(res);
      if (res.ok) {
        const noop = res.rolled_back !== undefined
          ? res.rolled_back === 0
          : !isRollbackDone(res.stdout_tail);
        if (noop) message.info(t('evolutionConsole.rollbackNoop'));
        else message.success(t('evolutionConsole.rollbackDone'));
      } else {
        message.error(
          t('evolutionConsole.rollbackFailed', {
            rc: res.rc,
            tail: (res.stderr_tail || res.stdout_tail || '-').slice(-200),
          }),
        );
      }
      await load();
      await loadVersions();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setBusy(false);
    }
  };

  if (loading && !bundle) {
    return (
      <div className="yx-card" style={{ textAlign: 'center', padding: 64 }}>
        <Spin />
      </div>
    );
  }
  if (error && !bundle) {
    return (
      <Alert type="error" showIcon message={error}
             action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
    );
  }
  if (!bundle) return null;

  const run = bundle.run;
  const stage = typeof run.stage === 'string' ? run.stage : '?';
  const { base, failed } = parseStage(stage);
  const unreadable = run.status === 'unreadable'; // [W2] 坏 run：不出 Steps（避免假状态）
  const dangerStage = /^failed@(parent-ran|applied|candidate-ran)$/.test(stage);
  const danger = bundle.stuck || dangerStage;
  const entries = Array.isArray(bundle.patch?.entries) ? bundle.patch.entries : [];
  const skipped = Array.isArray(bundle.patch?.skipped) ? bundle.patch.skipped : [];
  const candidates: CandidateItem[] = bundle.candidates ?? [];
  const smokePlan = preview?.smoke_plan;

  // ad-hoc 语料提升的唯一权威来源是**服务端返回的 candidates**：
  // `patch` 在 `_evolution_bundle` 被白名单投影，根本没有 `candidate` 字段；
  // preview 只含 unverified case，仅作候选缺失时的次级来源。
  const candidateAdhoc = (bundle.candidates ?? []).flatMap(
    (candidate) => candidate.evidence?.adhoc_cases ?? [],
  );
  const adhocCorpusCases: CandidateAdhocCase[] = (() => {
    const source = candidateAdhoc.length ? candidateAdhoc : (preview?.adhoc_cases ?? []);
    const seen = new Set<string>();
    return source.filter((item) => {
      if (!item?.case_id || seen.has(item.case_id)) return false;
      seen.add(item.case_id);
      return true;
    });
  })();
  const verifiedAdhoc = adhocCorpusCases.filter((item) => item.verification === 'verified');
  // 人工验真结论只带 verdict、没有 SQL truth（`manual_adhoc_outcomes`）：
  // 可以展示，但不能默认勾选（后端会记 skipped(no_sql_truth)）。
  const humanVerifiedAdhoc = verifiedAdhoc.filter((item) => item.verified_by === 'human');
  const promotableAdhoc = verifiedAdhoc.filter((item) => item.verified_by !== 'human');

  const openPromote = () => {
    setPromoteChecked(promotableAdhoc.map((item) => item.case_id));
    setPromoteConflicts([]);
    setPromoteOpen(true);
  };

  const closePromote = () => {
    setPromoteOpen(false);
    setPromoteConflicts([]);
  };

  const doPromote = async () => {
    if (!promoteChecked.length) {
      message.warning(t('evolutionConsole.promoteAdhocNone'));
      return;
    }
    setPromoting(true);
    try {
      const res = await promoteAdhoc(runId, promoteChecked);
      const conflicts = Array.isArray(res.conflicts) ? res.conflicts : [];
      const skippedRows = Array.isArray(res.skipped) ? res.skipped : [];
      // `no_sql_truth` 不是「重复/冲突」：单独一句说明，别混进冲突计数里。
      const noTruth = skippedRows.filter((item) => item.reason === 'no_sql_truth').length;
      const done = t('evolutionConsole.promoteAdhocDone', {
        created: res.created?.length ?? 0,
        skipped: skippedRows.length,
      });
      message.success(noTruth
        ? `${done} ${t('evolutionConsole.promoteAdhocNoTruthSkipped', { n: noTruth })}`
        : done);
      setPromoteConflicts(conflicts);
      // 冲突留在弹窗里给人看（不阻断创建结果），否则收口关闭。
      if (!conflicts.length) setPromoteOpen(false);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setPromoting(false);
    }
  };
  const overview = (
    <>
      <div className="yx-card" style={{ marginBottom: 16 }}>
        <Descriptions size="small" column={3}>
          <Descriptions.Item label={t('evolutionConsole.runKind')}>
            {run.kind
              ? t(`evolutionConsole.kind.${run.kind}`, { defaultValue: run.kind })
              : '-'}
          </Descriptions.Item>
          <Descriptions.Item label={t('evolutionConsole.runStatus')}>
            <Tag color={statusTagColor(run.status)}>
              {t(`evolutionConsole.status.${run.status}`, { defaultValue: run.status })}
            </Tag>
            {bundle.stale && <Tag color="warning">{t('evolutionConsole.staleTag')}</Tag>}
          </Descriptions.Item>
          <Descriptions.Item label="kb">{run.kb || '-'}</Descriptions.Item>
          <Descriptions.Item label="ds">{(run.ds as string) || '-'}</Descriptions.Item>
          <Descriptions.Item label={t('evolutionConsole.runCreated')}>
            {run.created_at ? run.created_at.replace('T', ' ').slice(0, 19) : '-'}
          </Descriptions.Item>
          <Descriptions.Item label={t('evolutionConsole.runUpdated')}>
            {run.updated_at ? run.updated_at.replace('T', ' ').slice(0, 19) : '-'}
          </Descriptions.Item>
        </Descriptions>
        {!unreadable && ( // [W2]
          <Steps
            size="small"
            style={{ marginTop: 8 }}
            current={stageIndex(stage)}
            status={failed ? 'error' : 'process'}
            items={STAGE_ORDER.map((s) => ({ title: t(stageLabelKey(s), { defaultValue: s }) }))}
          />
        )}
        {run.error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={run.error} />}
        <div style={{ marginTop: 8 }}>
          <Button size="small" icon={<ReloadOutlined />}
                  onClick={() => { void load(); void loadVersions(); }}>
            {t('common.refresh')}
          </Button>
        </div>
      </div>

      {danger && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('evolutionConsole.libSafetyBanner', { stage: base })}
          action={<Button danger loading={busy} disabled={runAdopting} onClick={() => openRollback()}>{t('evolutionConsole.rollback')}</Button>}
        />
      )}

      {adoptingLong && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('evolutionConsole.adoptStillRunning')}
        />
      )}

      <BenchCard
        bench={bundle.bench ?? null}
        benchStatus={typeof run.bench_status === 'string' ? run.bench_status : null}
        benchMode={typeof run.bench_mode === 'string' ? run.bench_mode : null}
      />

      <div className="yx-card" style={{ marginBottom: 16 }}>
        <h3>{t('evolutionConsole.gateTitle')}</h3>
        <GateCard
          gate={bundle.gate}
          gateEffective={bundle.gate_effective}
          gateAdhoc={bundle.gate_adhoc}
          override={bundle.override}
        />
      </div>

      <SmokeCard
        smoke={bundle.smoke}
        runSmokeStatus={typeof run.smoke_status === 'string' ? run.smoke_status : null}
      />

      {/* 横幅与列表只统计「可提升」的 case（人工验真没有 SQL truth，后端会
          skipped，默认也不勾选）；卡片本身在有 verified case 时就显示，让
          纯人工验真的 run 也能看到「为什么不能提升」。 */}
      {verifiedAdhoc.length > 0 && (
        <div className="yx-card" style={{ marginBottom: 16 }}>
          <h3>{t('evolutionConsole.promoteAdhocTitle')}</h3>
          <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
            {t('evolutionConsole.promoteAdhocHint', { n: promotableAdhoc.length })}
          </p>
          <ul style={{ margin: '0 0 12px', paddingLeft: 20 }}>
            {promotableAdhoc.map((item) => (
              <li key={item.case_id}>
                <code>{item.case_id}</code>
                {item.question ? ` · ${item.question}` : ''}
              </li>
            ))}
          </ul>
          <Button type="primary" onClick={openPromote}>
            {t('evolutionConsole.promoteAdhocAction')}
          </Button>
        </div>
      )}

      {bundle.report_md && (
        <div className="yx-card" style={{ marginBottom: 16 }}>
          <h3>{t('evolutionConsole.reportTitle')}</h3>
          <MarkdownLite text={bundle.report_md} />
        </div>
      )}

      <div className="yx-card" style={{ marginBottom: 16 }}>
        <h3>{t('evolutionConsole.entriesTitle')}</h3>
        <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
          {t('evolutionConsole.entriesHint')}
        </p>
        {entries.length === 0 ? (
          <Empty description={t('evolutionConsole.entriesEmpty')} />
        ) : (
          <>
            <Checkbox.Group
              value={selected}
              onChange={(v) => setSelected(v as string[])}
              style={{ display: 'block' }}
            >
              {entries.map((e) => {
                const edge = relationEdgeOf(e); // 非 null = relation 条目（source.col → target.col）
                // mapping 与 constraint 共用 add/update/disable/enable 词汇，必须按 run.kind
                // 分派，否则 mapping 条目会落进 constraint 专属的表达式渲染。
                const mappingEntry = run.kind === 'mapping';
                const toolEntry = run.kind === 'tool_schema';
                const cOp = run.kind === 'constraint' ? constraintOpOf(e) : null;
                const tOp = toolEntry ? toolSchemaOpOf(e) : null;
                const removeOp = relationOpOf(e);
                // constraint 的 add/update 有可改写的 expression（P1-4 收尾放开）；
                // disable/enable 只翻转启用状态，仍不给调整口。其余带 op 的
                // （relation / mapping / tool_schema）没有 alias/表达式语义，保持隐藏。
                // synonym 条目没有 alias/表达式（改词 = 改候选语料），同样不给调整口。
                const canAdjust = !toolEntry && edge === null
                  && run.kind !== 'synonym'
                  && (e.op === undefined
                    || (run.kind === 'constraint' && (cOp === 'add' || cOp === 'update')));
                const key = entryKeyOf(e, run.kind);
                // 同一工具可能出现两条（update + disable）：React key 必须带 op，
                // 否则重复 key 会让复选框联动/行错位；Checkbox 的 value 仍是 `key`。
                const rowKey = `${String(e.op ?? '')}:${key}`;
                const beforeExpression = e.before?.expression ?? undefined;
                return (
                  <div key={rowKey} style={{ marginBottom: 6 }}>
                    <Checkbox value={key}>
                      <code>{entryLabelOf(e, run.kind)}</code>
                      {mappingEntry && (
                        <>
                          <Tag style={{ marginLeft: 8 }}>{String(e.op ?? '')}</Tag>
                          <code style={{ marginLeft: 8 }}>{String(e.target_code ?? '')}</code>
                          {mappingSlotPairs(e).map((pair) => (
                            <span key={pair.slot} style={{ marginLeft: 8, color: '#8c8c8c' }}>
                              {t(`evolutionConsole.mappingSlot.${pair.slot}`, {
                                defaultValue: pair.slot,
                              })}：
                              {pair.before ?? '-'} → {pair.after ?? '-'}
                            </span>
                          ))}
                        </>
                      )}
                      {edge !== null && e.name && <Tag style={{ marginLeft: 8 }}>{e.name}</Tag>}
                      {removeOp && (
                        <Tag color="error" icon={<StopOutlined />} style={{ marginLeft: 8 }}>
                          {t('evolutionConsole.relationOp.remove')}
                        </Tag>
                      )}
                      {tOp && (
                        <Tag
                          color={tOp === 'disable' ? 'warning' : tOp === 'enable' ? 'success' : 'processing'}
                          icon={tOp === 'disable'
                            ? <StopOutlined />
                            : tOp === 'enable' ? <CheckCircleOutlined /> : undefined}
                          style={{ marginLeft: 8 }}
                        >
                          {t(`evolutionConsole.toolSchemaOp.${tOp}`)}
                        </Tag>
                      )}
                      {tOp === 'update' && (
                        <span style={{ color: '#8c8c8c', marginLeft: 8 }}>
                          {e.before?.description ? `${e.before.description} → ` : ''}
                          {e.description || '-'}
                        </span>
                      )}
                      {/* update：name: 旧 → 新（新表达式折叠在下方 details 里） */}
                      {cOp === 'update' && (
                        <span style={{ color: '#8c8c8c', marginLeft: 8 }}>
                          {beforeExpression || '-'} → {t('evolutionConsole.constraintNewShort')}
                        </span>
                      )}
                      {cOp === 'disable' && (
                        <Tag color="warning" icon={<StopOutlined />} style={{ marginLeft: 8 }}>
                          {t('evolutionConsole.constraintOp.disable')}
                        </Tag>
                      )}
                      {cOp === 'enable' && (
                        <Tag color="success" icon={<CheckCircleOutlined />} style={{ marginLeft: 8 }}>
                          {t('evolutionConsole.constraintOp.enable')}
                        </Tag>
                      )}
                      {e.proposal_id && <Tag style={{ marginLeft: 8 }}>{e.proposal_id}</Tag>}
                      {e.question && (
                        <span style={{ color: '#8c8c8c', marginLeft: 8 }}>「{e.question}」</span>
                      )}
                    </Checkbox>
                    {cOp === 'update' ? (
                      <details style={{ marginLeft: 24, fontSize: 12 }}>
                        <summary style={{ cursor: 'pointer' }}>
                          {t('evolutionConsole.constraintNewExpression')}
                        </summary>
                        <div style={{ color: '#595959' }}>{e.expression || '-'}</div>
                      </details>
                    ) : edge !== null ? (
                      // remove 行不再套「采纳后写入 relation_types」的加入提示（语义相反），
                      // 只有原边自带 description 时才展示。
                      (removeOp
                        ? e.description && (
                          <div style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 24 }}>
                            {e.description}
                          </div>
                        )
                        : (
                          <div style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 24 }}>
                            {e.description || t('evolutionConsole.relationEntryHint')}
                          </div>
                        ))
                    ) : (
                      e.expression && (
                        <div style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 24 }}>{e.expression}</div>
                      )
                    )}
                    {/* relation / constraint / mapping 条目没有 alias 语义（mapping 条目也带 op），
                        人工调整口（改别名/表达式）不适用 — 例外：constraint add/update 可改口径 */}
                    {canAdjust && (
                      <Tooltip title={run.status !== 'evaluated'
                        ? t('evolutionConsole.adjustDisabledHint') : ''}>
                        <Button size="small" type="link" style={{ marginLeft: 24 }}
                                disabled={run.status !== 'evaluated'}
                                onClick={() => setAdjustTarget(e)}>
                          {t('evolutionConsole.adjustAction')}
                        </Button>
                      </Tooltip>
                    )}
                  </div>
                );
              })}
            </Checkbox.Group>
            {skipped.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary style={{ cursor: 'pointer' }}>
                  {t('evolutionConsole.skippedTitle')}（{skipped.length}）
                </summary>
                <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                  {skipped.map((s, i) => (
                    <li key={i}>
                      <code>{skipItemLabel(s)}</code>：{humanizeSkipReason(t, s, entries)}
                      {s.why && (
                        <span style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 4 }}>（{s.why}）</span>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <Space style={{ marginTop: 12 }} wrap>
              <Button type="primary" disabled={!selected.length || runAdopting} loading={previewing} onClick={doPreview}>
                {t('evolutionConsole.preview')}
              </Button>
              <Input
                style={{ width: 280 }}
                placeholder={t('evolutionConsole.rejectNote')}
                value={rejectNote}
                onChange={(e) => setRejectNote(e.target.value)}
              />
              <Button danger loading={busy} disabled={runAdopting} onClick={doReject}>
                {t('evolutionConsole.rejectAction')}
              </Button>
              <Tooltip title={run.status === 'dry-run' ? t('evolutionConsole.rollbackDryRunBlocked') : ''}>
                <Button loading={busy} disabled={run.status === 'dry-run' || runAdopting} onClick={() => openRollback()}>
                  {t('evolutionConsole.rollback')}
                </Button>
              </Tooltip>
            </Space>
          </>
        )}
        {/* 202 的「已受理」只在后台仍在跑时展示；run 到终态后自动消失
            （否则会与随后的 adopted / smoke-failed 结论并存、互相矛盾）。 */}
        {adoptResult && (!adoptResult.accepted || runAdopting) && (
          <Alert
            style={{ marginTop: 12 }}
            type={adoptResult.accepted ? 'info' : adoptResult.ok ? 'success' : adoptResult.timeout ? 'error' : isNoopResult(adoptResult) ? 'info' : 'warning'}
            showIcon
            message={
              adoptResult.accepted
                ? t('evolutionConsole.adoptAccepted')
                : adoptResult.ok
                  ? t('evolutionConsole.adoptOk')
                  : adoptResult.timeout
                    ? t('evolutionConsole.adoptTimeout')
                    : isNoopResult(adoptResult)
                      ? t('evolutionConsole.adoptNoop')
                      : t('evolutionConsole.adoptFailed', { rc: adoptResult.rc })
            }
            description={
              <div>
                {!adoptResult.accepted && (
                  <div>
                    {t('evolutionConsole.readbackLabel')}：
                    {adoptResult.run?.status === 'adopted'
                      ? t('evolutionConsole.readbackOk')
                      : t('evolutionConsole.readbackFail')}
                  </div>
                )}
                {adoptResult.version_id && (
                  <div>
                    {t('evolutionConsole.rollbackResultVersion')}：<code>{adoptResult.version_id}</code>
                    {adoptResult.idempotent_replay && (
                      <Tag style={{ marginLeft: 8 }}>{t('evolutionConsole.idempotentReplay')}</Tag>
                    )}
                  </div>
                )}
                {adoptResult.gate_effective && (
                  <div>{t('evolutionConsole.gateEffective')}：<code>{adoptResult.gate_effective}</code></div>
                )}
                {adoptResult.smoke && (
                  <>
                    <b>{t('evolutionConsole.smokeTitle')}</b>
                    <span style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 4 }}>
                      {t('evolutionConsole.smokeNote')}
                    </span>
                    {Array.isArray((adoptResult.smoke as SmokeResult).cases)
                      || typeof (adoptResult.smoke as SmokeResult).status === 'string' ? (
                        <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                          {(adoptResult.smoke as SmokeResult).reasons?.map((r, i) => (
                            <li key={i}>
                              <code>{r.case_id ?? '-'}</code>：
                              {r.reason
                                ? t(`evolutionConsole.smokeReason.${r.reason}`, { defaultValue: r.reason })
                                : '-'}
                            </li>
                          ))}
                          {(adoptResult.smoke as SmokeResult).cases?.map((c, i) => (
                            <li key={c.case_id ?? i}>
                              <code>{c.case_id ?? '-'}</code>：
                              <Tag color={outcomeColor(c.outcome)}>
                                {t(`evolutionConsole.smoke.${c.outcome}`, { defaultValue: c.outcome ?? '-' })}
                              </Tag>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                          {Object.entries(adoptResult.smoke as Record<string, string>).map(([c, v]) => (
                            <li key={c}>
                              <code>{c}</code>：
                              <Tag color={v === 'pass' ? 'success' : v === 'fail' ? 'error' : 'warning'}>
                                {t(`evolutionConsole.smoke.${v}`, { defaultValue: v })}
                              </Tag>
                            </li>
                          ))}
                        </ul>
                      )}
                  </>
                )}
                {Array.isArray(adoptResult.warnings) && adoptResult.warnings.length > 0 && (
                  <Alert
                    style={{ marginTop: 8 }}
                    type="warning"
                    showIcon
                    message={t('evolutionConsole.adoptWarnings')}
                    description={(
                      <ul style={{ margin: 0, paddingLeft: 20 }}>
                        {adoptResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
                      </ul>
                    )}
                  />
                )}
                {!adoptResult.ok && !isNoopResult(adoptResult) && <div>{t('evolutionConsole.cliFailedHint')}</div>}
                {adoptResult.stdout_tail && (
                  <details>
                    <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.stdoutTail')}</summary>
                    <SqlPre text={adoptResult.stdout_tail} maxHeight={240} />
                  </details>
                )}
                {adoptResult.stderr_tail && (
                  <details>
                    <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.stderrTail')}</summary>
                    <SqlPre text={adoptResult.stderr_tail} maxHeight={240} />
                  </details>
                )}
              </div>
            }
          />
        )}
      </div>

      <div className="yx-card" style={{ marginBottom: 16 }}>
        <Collapse
          activeKey={logOpen ? ['log'] : []}
          onChange={(keys) => (keys.length ? openLog() : setLogOpen(false))}
          items={[{
            key: 'log',
            label: t('evolutionConsole.logTitle'),
            children: logLines === null
              ? <Spin />
              : (logLines.length
                  ? <SqlPre text={logLines.join('\n')} maxHeight={400} />
                  : <Empty description={t('evolutionConsole.logEmpty')} />),
          }]}
        />
      </div>

    </>
  );

  const candidatesTab = candidates.length === 0
    ? <Empty description={t('evolutionConsole.candidatesEmpty')} />
    : (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {candidates.map((candidate) => (
          <CandidateCard key={candidate.id ?? `${candidate.candidate_no}`} candidate={candidate} />
        ))}
      </Space>
    );

  const versionsTab = (
    <div className="yx-card">
      {versionsError && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('evolutionConsole.versionsLoadFailed')}
          description={`${versionsError} · ${t('evolutionConsole.versionsFallback')}`}
          action={<Button size="small" onClick={() => void loadVersions()}>{t('common.retry')}</Button>}
        />
      )}
      {versionsLoading && versions.length === 0
        ? <Spin />
        : (
          <VersionList
            items={versions}
            runStatus={run.status}
            rollbackDisabled={run.status === 'dry-run' || runAdopting}
            onRollback={(version) => openRollback(version.version_id)}
          />
        )}
    </div>
  );

  return (
    <div>
      <div className="yx-page-title">
        <Space>
          <Button onClick={() => navigate('/evolution')}>{t('evolutionConsole.backToList')}</Button>
          <h1 style={{ margin: 0 }}>{t('evolutionConsole.detailTitle', { runId })}</h1>
        </Space>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'overview', label: t('evolutionConsole.tabOverview'), children: overview },
          { key: 'candidates', label: t('evolutionConsole.tabCandidates'), children: candidatesTab },
          { key: 'versions', label: t('evolutionConsole.tabVersions'), children: versionsTab },
        ]}
      />

      <Modal
        open={previewOpen}
        title={t('evolutionConsole.previewTitle')}
        onCancel={() => setPreviewOpen(false)}
        onOk={doConfirmAdopt}
        confirmLoading={adopting}
        okText={t('evolutionConsole.confirmWrite')}
        okButtonProps={{ danger: true, disabled: adoptBlocked || runAdopting }}
        width={680}
      >
        {previewOpen && !run.parent_version_id && (
          <Alert type="info" showIcon style={{ marginBottom: 12 }}
                 message={t('evolutionConsole.firstAdoptV0Hint')} />
        )}
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
               message={t('evolutionConsole.confirmWriteWarn')} />
        {preview?.already_adopted && (
          <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('evolutionConsole.alreadyAdopted')} />
        )}

        <div style={{ marginBottom: 12 }}>
          <Space size={8} wrap>
            <span>{t('evolutionConsole.previewGate')}：</span>
            <Tag color={preview?.gate?.decision === 'accept' ? 'success' : preview?.gate?.decision === 'reject' ? 'error' : 'warning'}>
              {preview?.gate?.decision ?? '-'}
            </Tag>
            <span>{t('evolutionConsole.previewGateEffective')}：</span>
            <Tag color={preview?.gate_effective === 'accept' ? 'success' : preview?.gate_effective === 'reject' ? 'error' : 'warning'}>
              {preview?.gate_effective ?? '-'}
            </Tag>
          </Space>
        </div>

        {preview?.adhoc_veto && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }}
                 message={t('evolutionConsole.previewAdhocVeto')} />
        )}
        {preview?.override_required && (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                 message={t('evolutionConsole.previewOverrideRequired')}
                 description={
                   <Input.TextArea
                     rows={2}
                     value={overrideReason}
                     placeholder={t('evolutionConsole.previewOverridePlaceholder')}
                     onChange={(e) => setOverrideReason(e.target.value)}
                   />
                 } />
        )}
        {adhocCases.length > 0 && (
          <Alert
            type={adhocFailed.length ? 'error' : adhocMissing.length ? 'warning' : 'success'}
            showIcon
            style={{ marginBottom: 12 }}
            message={adhocCopy.cases}
            description={adhocFailed.length
              ? t('evolutionConsole.previewAdhocFailVeto')
              : adhocMissing.length ? adhocCopy.missing : adhocCopy.ready}
            action={
              <Button size="small" onClick={() => setAdhocOpen(true)}>
                {adhocCopy.fill}
              </Button>
            }
          />
        )}
        {smokePlan && (
          <div style={{ marginBottom: 12 }}>
            <div>
              {t('evolutionConsole.previewSmokePlan')}：
              {t('evolutionConsole.previewSmokeEstimated', {
                n: smokePlan.estimated_runs ?? (smokePlan.cases?.length ?? 0),
              })}
            </div>
            {(smokePlan.cases?.length ?? 0) > 0 && (
              <details>
                <summary style={{ cursor: 'pointer' }}>
                  {t('evolutionConsole.smokeCases')}（{smokePlan.cases?.length ?? 0}）
                </summary>
                <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                  {(smokePlan.cases ?? []).map((item, i) => (
                    <li key={item.name ?? i}>
                      <code>{item.name ?? '-'}</code>
                      {item.question ? `：${item.question}` : ''}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}

        <p>
          {t('evolutionConsole.previewPlanned', { n: preview?.planned.length ?? 0 })}；
          {t('evolutionConsole.previewSkipped', { n: preview?.skipped.length ?? 0 })}
        </p>
        <ul style={{ paddingLeft: 20 }}>
          {(preview?.planned ?? []).map((p, i) => (
            <li key={i}><code>{plannedLabel(p, run.kind)}</code></li>
          ))}
        </ul>
        {(preview?.skipped ?? []).length > 0 && (
          <details>
            <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.skippedTitle')}</summary>
            <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
              {(preview?.skipped ?? []).map((s, i) => (
                <li key={i}>
                  <code>{skipItemLabel(s)}</code>：{humanizeSkipReason(t, s, [], { apply: true })}
                  {s.why && (
                    <span style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 4 }}>（{s.why}）</span>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
        <p style={{ color: '#8c8c8c', fontSize: 12 }}>
          {t('evolutionConsole.previewBackup')}：{preview?.backup_hint}
        </p>
      </Modal>

      <Modal
        open={rollbackOpen}
        title={t('evolutionConsole.rollbackTitle')}
        onCancel={() => setRollbackOpen(false)}
        onOk={doRollback}
        confirmLoading={busy}
        okText={t('evolutionConsole.rollback')}
        okButtonProps={{ danger: true, disabled: rollbackRisky && !rollbackAck }}
        width={640}
      >
        <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
          {t('evolutionConsole.rollbackPickHint')}
        </p>
        <Select
          style={{ width: '100%', marginBottom: 12 }}
          allowClear
          value={rollbackTarget}
          onChange={(value) => {
            setRollbackTarget(value ?? undefined);
            setRollbackAck(false);
          }}
          placeholder={t('evolutionConsole.rollbackDefaultTarget')}
          options={versions.map((version) => ({
            value: version.version_id,
            label: [
              version.version_id,
              version.status,
              isLegacyVersion(version) ? 'legacy' : '',
              isArchivedVersion(version) ? 'archived' : '',
            ].filter(Boolean).join(' · '),
          }))}
        />
        {rollbackRisky && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={isLegacyVersion(selectedRollbackVersion as SemanticVersion)
              ? t('evolutionConsole.rollbackTargetLegacyWarn')
              : t('evolutionConsole.rollbackTargetArchivedWarn')}
            description={
              <Checkbox checked={rollbackAck} onChange={(e) => setRollbackAck(e.target.checked)}>
                {t('evolutionConsole.rollbackAck')}
              </Checkbox>
            }
          />
        )}
        <Input.TextArea
          rows={3}
          value={rollbackReason}
          placeholder={t('evolutionConsole.rollbackReasonPlaceholder')}
          onChange={(e) => setRollbackReason(e.target.value)}
        />
        {rollbackResult && (
          <Alert
            style={{ marginTop: 12 }}
            type={rollbackResult.ok ? 'success' : 'error'}
            showIcon
            message={rollbackResult.ok
              ? t('evolutionConsole.rollbackDone')
              : t('evolutionConsole.rollbackFailed', {
                  rc: rollbackResult.rc,
                  tail: (rollbackResult.stderr_tail || rollbackResult.stdout_tail || '-').slice(-200),
                })}
            description={
              <div>
                {rollbackResult.version_id && (
                  <div>{t('evolutionConsole.rollbackResultVersion')}：<code>{rollbackResult.version_id}</code></div>
                )}
                {rollbackResult.target_version_id && (
                  <div>{t('evolutionConsole.rollbackResultTarget')}：<code>{rollbackResult.target_version_id}</code></div>
                )}
                <Space size={4} wrap>
                  {rollbackResult.legacy && <Tag color="warning">{t('evolutionConsole.versionLegacyTag')}</Tag>}
                  {rollbackResult.archived && <Tag>{t('evolutionConsole.versionArchivedTag')}</Tag>}
                  {rollbackResult.rolled_back !== undefined && (
                    <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                      count={rollbackResult.rolled_back}
                    </span>
                  )}
                </Space>
                {(rollbackResult.warnings?.length ?? 0) > 0 && (
                  <>
                    <b>{t('evolutionConsole.rollbackWarnings')}</b>
                    <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                      {(rollbackResult.warnings ?? []).map((warning, i) => (
                        <li key={i}>{warning}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            }
          />
        )}
      </Modal>

      {run.kind === 'alias' || run.kind === 'synonym' ? (
        // alias / synonym 批人工验真：先看候选生效后的真实返回，再判 pass/fail。
        <AdhocVerifyModal
          open={adhocOpen}
          runId={runId}
          cases={adhocCases}
          picks={selected}
          submitting={adopting}
          onCancel={() => {
            setAdhocOpen(false);
            // 取消验真不吞掉预览：planned/skipped 仍可见，确认按钮保持禁用。
            setPreviewOpen(true);
          }}
          onSubmit={submitAdhocVerdicts}
        />
      ) : (
        <AdhocTruthModal
          open={adhocOpen}
          cases={adhocCases}
          value={adhocTruths}
          submitting={adopting}
          onCancel={() => {
            setAdhocOpen(false);
            // 取消补 truth 不吞掉预览：仍展示 planned/skipped，只是确认按钮保持禁用。
            setPreviewOpen(true);
          }}
          onSubmit={submitAdhocTruths}
        />
      )}

      <Modal
        open={promoteOpen}
        title={t('evolutionConsole.promoteAdhocModalTitle')}
        onCancel={closePromote}
        onOk={promoteConflicts.length ? closePromote : doPromote}
        confirmLoading={promoting}
        okText={promoteConflicts.length
          ? t('evolutionConsole.promoteAdhocClose')
          : t('evolutionConsole.promoteAdhocSubmit')}
        okButtonProps={{
          disabled: !promoteConflicts.length && !promoteChecked.length,
        }}
        width={640}
        destroyOnHidden
      >
        <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 12px' }}>
          {t('evolutionConsole.promoteAdhocPickHint')}
        </p>
        {humanVerifiedAdhoc.length > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('evolutionConsole.promoteAdhocHumanHint', {
              n: humanVerifiedAdhoc.length,
            })}
          />
        )}
        <Checkbox.Group
          value={promoteChecked}
          onChange={(v) => setPromoteChecked(v as string[])}
          style={{ display: 'block' }}
        >
          {verifiedAdhoc.map((item) => (
            <div key={item.case_id} style={{ marginBottom: 6 }}>
              <Checkbox value={item.case_id}>
                <code>{item.case_id}</code>
                {item.question && <span style={{ marginLeft: 8 }}>{item.question}</span>}
              </Checkbox>
            </div>
          ))}
        </Checkbox.Group>
        {promoteConflicts.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 12 }}
            message={t('evolutionConsole.promoteAdhocConflicts', { n: promoteConflicts.length })}
            description={(
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {promoteConflicts.map((conflict) => (
                  <li key={conflict.case_id}>
                    {conflict.question ? `${conflict.question} — ` : ''}
                    <code>{conflict.case_id}</code>
                    <span style={{ margin: '0 4px' }}>
                      {t('evolutionConsole.promoteAdhocExisting')}
                    </span>
                    <code>{conflict.existing_case_id}</code>
                  </li>
                ))}
              </ul>
            )}
          />
        )}
      </Modal>

      <EntryAdjustModal
        open={adjustTarget !== null}
        runId={runId}
        entry={adjustTarget ?? {}}
        onClose={() => setAdjustTarget(null)}
        onApplied={load}
      />
    </div>
  );
}
