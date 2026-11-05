import type { AxiosRequestConfig } from 'axios';
import apiClient from './request';

// ── 服务端数据形状（与 `/api/v1/knowledge-base/evolution` 的响应逐字段对齐）──

export interface EvolutionRunSummary {
  run_id: string;
  kind: string | null;
  kb: string | null;
  ds: string | null;
  stage: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  error: string;
  stuck: boolean;
  stale: boolean;
  gate: { decision: string; delta: number; passes: number[] } | null;
  entries_count: number | null;
  target_cases: string[] | null;
  /** 异步采纳的完整语料基准状态（列表列用；详情以 RunBundle.bench_status 为准）。 */
  bench_status?: string | null;
}

/** gate.json 全量（RunDetail 门结论卡用；形状取自 tests/eval/nl2sql/gate.py `decide`）。
 * decision ∈ accept / reject / uncertain。 */
export interface GateTarget {
  case: string;
  parent: string | null;
  candidate: string | null;
  ok: boolean;
}

/** by_tier：`{tier: {parent: {pass,fail,error}, candidate: {pass,fail,error}}}`。 */
export interface TierSlot {
  pass: number;
  fail: number;
  error: number;
}

export interface GateDetail {
  decision: string;
  delta: number;
  passes: number[];
  by_tier?: Record<string, { parent: TierSlot; candidate: TierSlot }>;
  new_red?: string[];
  new_error?: string[];
  targets?: GateTarget[];
  jitter?: { parent?: string[]; candidate?: string[]; targets?: string[] };
  probe?: Record<string, { identical: number; total: number; question?: string }>;
  reasons?: string[];
}

/** 信任闭环 P0：候选 evidence 里的一条失败观测（spec §3.5）。 */
export interface CandidateObservation {
  id?: string | null;
  question?: string | null;
  sql?: string | null;
  rejected_sql?: string | null;
  reason_code?: string | null;
  row_count?: number | null;
}

/** 候选 evidence 里自动生成的 ad-hoc case（语料未命中 → verification=unverified）。 */
export interface CandidateAdhocCase {
  case_id: string;
  question?: string | null;
  verification?: string | null;
  /** `human` = 人审判定（只有结论、没有 SQL truth，提升时默认不勾选）。 */
  verified_by?: string | null;
  failure_signature?: Record<string, unknown> | null;
  truth?: unknown;
  [key: string]: unknown;
}

export interface CandidateEvidence {
  observations?: CandidateObservation[];
  probe?: Record<string, unknown> | null;
  dislike_ids?: string[];
  adhoc_cases?: CandidateAdhocCase[];
  source?: string | null;
  [key: string]: unknown;
}

/** 期望变更：父本 fail → 候选 pass / changed。 */
export interface ExpectedChange {
  case_id: string;
  from?: string | null;
  to?: string | null;
}

/** `evolution_candidates` 行（RunBundle.candidates[]）。 */
export interface CandidateItem {
  id?: string;
  run_id?: string;
  candidate_no: number;
  parent_version_id?: string | null;
  layer?: string | null;
  edit_type?: string | null;
  target?: unknown;
  before?: unknown;
  after?: unknown;
  expected_changes?: ExpectedChange[] | null;
  rationale?: string | null;
  evidence?: CandidateEvidence | null;
  gate_decision?: string | null;
  gate_reasons?: unknown;
  status?: string | null;
  created_at?: string | null;
}

/** 语义版本摘要（`versions[]` / `/versions`；默认不含 snapshot）。
 * 后端 `_version_dict` 目前给 `kb` / `ds`；同时兼容 `data_source_id` 别名。 */
export interface SemanticVersion {
  version_id: string;
  kb?: string | null;
  scope: string;
  ds?: string | null;
  data_source_id?: string | null;
  parent_version_id?: string | null;
  source_run_id?: string | null;
  source_version_id?: string | null;
  snapshot_hash?: string | null;
  status: string;
  /** 后端给 int(0/1)；兼容布尔。 */
  legacy?: boolean | number | null;
  archived?: boolean | null;
  note?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  activated_at?: string | null;
  snapshot?: unknown;
}

/** smoke 单题结果（后端 `run_smoke` 的 cases[]）。 */
export interface SmokeCaseResult {
  case_id?: string;
  kind?: string;
  question?: string | null;
  verification?: string | null;
  outcome?: string | null;
  reason_code?: string | null;
  sql?: string | null;
  why?: string | null;
  [key: string]: unknown;
}

/** smoke 失败原因（reason ∈ target_not_pass / control_regression / new_error / …）。 */
export interface SmokeReason {
  case_id?: string | null;
  reason?: string | null;
  [key: string]: unknown;
}

/** 采纳后 smoke（`run.smoke`）。旧 run 还可能是 `{case: outcome}` 映射，渲染需兼容两种形状。 */
export interface SmokeResult {
  mode?: string | null;
  status?: string | null;
  cases?: SmokeCaseResult[];
  reasons?: SmokeReason[];
  duration_ms?: number | null;
  version_id?: string | null;
  planned?: number | null;
  executed?: number | null;
  [key: string]: unknown;
}

/** 异步采纳的完整语料基准（bench.py / adopt_service 落库形状）。
 * `bench_status` ∈ pending / parent_ran / passed / failed / warn / skipped。 */
export type BenchStatus =
  | 'pending' | 'parent_ran' | 'passed' | 'failed' | 'warn' | 'skipped' | null;

/** 基准单题结果（`slim_case`：SQL 已截断）。 */
export interface BenchCase {
  name: string;
  outcome: string;
  best?: string | null;
  reason_code?: string | null;
  sql?: string | null;
  rejected_sql?: string | null;
  row_count?: number | null;
  elapsed_ms?: number | null;
}

/** 基准单臂：`graded` = pass+fail（error 单列，不入分母）。 */
export interface BenchArm {
  passes: number;
  graded: number;
  errors: number;
  cases: BenchCase[];
}

/** 采纳时跑的两臂语料基准（`run.bench`）。 */
export interface BenchResult {
  mode?: string | null;
  corpus_size?: number | null;
  parent?: BenchArm | null;
  candidate?: BenchArm | null;
  new_red?: string[];
  new_error?: string[];
  /** 跨臂 ruler 变化的面（后端按字母序写 `abstract` / `compiled`）：两臂跑的不是同一把尺。 */
  ruler_crossed?: string[];
  flaky?: { name: string; first?: string | null; retry?: string | null }[];
  reasons?: string[];
  duration_ms?: number | null;
  attempt?: number | null;
  override?: boolean;
  bench_mode?: string | null;
}

/** gate override 记录（RunBundle.override）。 */
export interface OverrideInfo {
  used?: boolean;
  by?: string | null;
  reason?: string | null;
}

/** 预览里待补 truth 的 ad-hoc case（AdoptPreview.adhoc_cases[]）。 */
export interface AdhocCase {
  case_id: string;
  question?: string | null;
  verification?: string | null;
  /** `human` = 人审判定（只有结论、没有 SQL truth，提升时默认不勾选）。 */
  verified_by?: string | null;
  failure_signature?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** 提升 ad-hoc 语料：`created` 是新建的语料行（后端 `_case_dict` + id）。 */
export interface PromoteAdhocCreated {
  id: string;
  name: string;
  question: string;
  tier: string;
  expect: string;
  control?: string[];
}

/** 提升 ad-hoc 语料：与既有语料冲突而被跳过的条目。 */
export interface PromoteAdhocConflict {
  case_id: string;
  question?: string | null;
  /** 语料里已存在的行 id（冲突对象）。 */
  existing_case_id: string;
}

/** 提升 ad-hoc 语料：被跳过（重复/非法）的条目。 */
export interface PromoteAdhocSkipped {
  case_id: string;
  reason: string;
}

/** `POST /runs/{id}/promote-adhoc` 响应（data 段）。 */
export interface PromoteAdhocResult {
  created: PromoteAdhocCreated[];
  skipped: PromoteAdhocSkipped[];
  conflicts: PromoteAdhocConflict[];
}

/** 采纳时补的 truth（`adhoc_truths[]`）：control 每项一条 SQL。 */
export interface AdhocTruth {
  case_id: string;
  control: string[];
  expect: 'answer' | 'refuse';
}

/** 人工 ad-hoc 验真的取数结果（`POST /runs/{run_id}/adhoc-evidence` 的 data 段）。
 * `evidence_id` 绑定「本次 picks 选中的条目 + case_id + 签发时间」：改 picks 或换候选
 * 都会让旧票据失效，所以**没有它不能判 pass**。空结果 / 拒答仍会签发票据（`ok=false`
 * 但 `error` 给出原因），只有 apply / ask 执行异常才没有票据。
 *
 * alias 批给 `alias` / `concept`；synonym 批（值同义词）给 `term` / `canonical`
 * —— 两组键按 kind 二选一，**不是**都有。 */
export interface AdhocEvidence {
  case_id: string;
  question: string;
  /** alias 批：候选别名（synonym 批没有该键）。 */
  alias?: string;
  /** alias 批：抽象概念名（synonym 批没有该键）。 */
  concept?: string;
  /** synonym 批：显示用的候选值同义词（优先取非 canonical 的第一个词）。 */
  term?: string;
  /** synonym 批：该组全部词条（含 canonical 本身），卡片按此完整展示。 */
  terms?: string[];
  /** synonym 批：标准值（归一目标）。 */
  canonical?: string;
  /** 抽象 schema 里该概念的其它别名（人工核对全名用）。 */
  concept_aliases?: string[];
  columns: string[];
  rows: unknown[];
  row_count: number;
  /** 行内容指纹（sha256）：判定只落摘要，不落完整 rows。 */
  rows_hash: string;
  ok: boolean;
  error: string | null;
  evidence_id: string | null;
}

/** 人工 ad-hoc 判定：`pass` 必须带证据票据；`fail` 可附理由。
 * `row_count` / `rows_hash` 是「当时看到的数据摘要」，随判定落到候选 evidence
 * （不落完整 rows）；旧调用方可以不带。 */
export interface AdhocVerdict {
  case_id: string;
  verdict: 'pass' | 'fail';
  evidence_id?: string;
  note?: string;
  row_count?: number;
  rows_hash?: string;
}

/** 预览的 smoke 计划（后端 `plan_smoke`，cases 是语料 case 摘要）。 */
export interface SmokePlanCase {
  name?: string;
  question?: string | null;
  tier?: string | null;
  expect?: string | null;
  [key: string]: unknown;
}

export interface SmokePlan {
  cases?: SmokePlanCase[];
  estimated_runs?: number;
  [key: string]: unknown;
}

export interface EvalCase {
  name: string;
  tier?: string;
  expect?: string;
  question?: string;
  outcome: string;
  best?: string;
  reason_code?: string | null;
  sql?: string | null;
  rejected_sql?: string | null;
  sql_has_join?: boolean;
  why?: string | null;
}

export interface EvalArm {
  summary: Record<string, unknown> | null;
  cases: EvalCase[];
}

/** 演化 run 的 kind：`alias` = 抽象别名批；`query_rule` = 查询规则批；
 * `relation` = join 边批；`constraint` = 约束批（relation/constraint 与 query_rule
 * 共用 compiled 版本链，scope=compiled，同链同锁）；`mapping` = 落点语义槽批、
 * `tool_schema` = 多轮工具口径批（二者与 alias 共用 abstract 版本链，KB 级 run，
 * 不接受 ds）；`synonym` = 值同义词批（KB 级、**独立** synonym 版本链，不接受 ds）。 */
export type EvolutionKind =
  | 'alias' | 'query_rule' | 'relation' | 'constraint' | 'mapping' | 'tool_schema'
  | 'synonym';

/** constraint 批的请求条目（P1-4）。四种 op 互斥：
 * - `add`：新增查询规则（只允许 constraint_type=query_rule）
 * - `update`：按 name 改 expression
 * - `disable` / `enable`：按 name 切换 enabled（没有 remove） */
export type ConstraintEntry =
  | { op: 'add'; target_code: string; constraint_type: 'query_rule'; expression: string }
  | { op: 'update'; name: string; expression: string }
  | { op: 'disable'; name: string }
  | { op: 'enable'; name: string };

/** relation 批的请求条目（P1 收尾）：只支持按边名删除。
 * 一条都不带 = 发现器模式（后端自动发现候补边）；remove 与 add 不能混批。 */
export type RelationEntry = { op: 'remove'; name: string };

/** tool_schema 批的请求条目（Tool-Schema 线，spec D2）：只改工具 description 与启停。
 * 工具由代码定义（`multiturn.TOOL_SCHEMAS`），**没有 add / remove**：
 * - `update`：按 name 改 description；
 * - `disable`：写 `enabled:false`；`enable`：删 `enabled` 键（幂等）。 */
export type ToolSchemaEntry =
  | { op: 'update'; name: string; description: string }
  | { op: 'disable'; name: string }
  | { op: 'enable'; name: string };

/** mapping 落点的四个语义槽（P1-5）。`enabled` 不在其中：它由 disable/enable 单独维护。 */
export type MappingSlot = 'role' | 'filter' | 'aggregation' | 'grain';

/** mapping 落点身份：抽象锚点（`target_code`）+ 物理三元组。 */
export interface MappingIdentity {
  target_code: string;
  data_source_id: string;
  entity_name: string;
  attribute_name: string;
}

/** mapping 批的请求条目（P1-5，spec §D6）。四类 op 互斥，不做物理删除。 */
export type MappingEntry =
  | ({ op: 'add' | 'update' } & MappingIdentity & Partial<Record<MappingSlot, string>>)
  | ({ op: 'disable' | 'enable' } & MappingIdentity);

/** compiled schema READ 口的约束行（`data.constraints[]`）。
 * `enabled` 只有显式 `false` 才算停用（R1 fail-open：缺省/其它值一律视作启用）。 */
export interface CompiledConstraint {
  name: string;
  target_type?: string | null;
  target_code?: string | null;
  constraint_type?: string | null;
  expression?: string | null;
  enabled?: boolean | null;
}

/** 条目落库前后的快照：constraint 用 expression / enabled；
 * mapping 用四个语义槽（`before` = 被改槽的旧值，`after` = 应用后的现存槽）。 */
export interface PatchEntryBefore {
  expression?: string | null;
  constraint_type?: string | null;
  enabled?: boolean | null;
  /** tool_schema 的 before = 原工具覆盖项（可能带 description / enabled）。 */
  description?: string | null;
  role?: string | null;
  filter?: string | null;
  aggregation?: string | null;
  grain?: string | null;
  /** relation remove 的 pre-image（原边对象浅拷贝）：回滚按它复原。 */
  name?: string | null;
  origin?: string | null;
}

export interface PatchEntry {
  concept?: string;
  alias?: string;
  /** synonym 条目：一组等价值词（`审批通过` / `已通过` …）。 */
  terms?: string[];
  /** synonym 条目：这组词归一到的标准值（`approved`）。 */
  canonical?: string;
  proposal_id?: string;
  question?: string;
  target_code?: string;
  expression?: string;
  raw_description?: string;
  /** constraint / mapping 条目：add / update / disable / enable；
   * relation 收尾批的 remove 条目：`{op:"remove", name}`（发现器条目没有 op）。 */
  op?: string;
  /** constraint update/disable 或 mapping update/disable/enable 的变更前快照。 */
  before?: PatchEntryBefore | null;
  /** mapping 应用后的现存槽（apply 记账产物；planned 期只有 `changed`）。 */
  after?: PatchEntryBefore | null;
  /** mapping update 的逐槽变更（planned 期产物；值 null = 显式清空）。 */
  changed?: Record<string, string | null> | null;
  /** mapping 落点物理三元组（target_code 是抽象锚点）。 */
  data_source_id?: string;
  entity_name?: string;
  attribute_name?: string;
  /** mapping add/update 的槽（planned 期原样携带，apply 后折叠进 after）。 */
  role?: string | null;
  filter?: string | null;
  aggregation?: string | null;
  grain?: string | null;
  constraint_type?: string;
  /** relation 条目：边的两端实体与列（`name` 是边名，缺省由后端按两端生成）。 */
  source?: string;
  source_column?: string;
  target?: string;
  target_column?: string;
  name?: string;
  cardinality?: string;
  description?: string;
}

/** patch.skipped / preview.skipped 条目形状（evolve.py 草稿期与落库期产出）。 */
export interface SkipItem {
  concept?: string;
  alias?: string;
  /** synonym 跳过条目：原始词组与标准值（`why` = empty_terms / empty_canonical / exists …）。 */
  terms?: string[];
  canonical?: string;
  /** synonym 跨组冲突时后端带出的冲突词（`why=cross_group_duplicate`）。 */
  term?: string;
  target_code?: string;
  /** relation 跳过的条目按两端描述（与 PatchEntry 同形；后端可能只带一端）。 */
  source?: string;
  source_column?: string;
  target?: string;
  target_column?: string;
  name?: string;
  /** 条目在请求 entries 里的下标（跳过条目必带；用于没有其它可展示字段时兜底）。 */
  index?: number;
  why?: string;
}

export interface PatchInfo {
  patch_id: string;
  kind: string;
  kb: string;
  ds: string | null;
  entries: PatchEntry[];
  skipped: SkipItem[];
  target_cases: string[];
  rollback_added: unknown[];
}

export interface Artifact {
  name: string;
  size: number;
  mtime: string;
}

export interface RunBundle {
  run: EvolutionRunSummary & Record<string, unknown>;
  stuck: boolean;
  stale: boolean;
  report_md: string | null;
  gate: GateDetail | null;
  patch: PatchInfo | null;
  eval: { parent: EvalArm | null; candidate: EvalArm | null };
  probe: Record<string, { identical: number; total: number }> | null;
  /** 旧 run 是 `{case: outcome}` 映射，新链路是 SmokeResult；两种都要能渲染。 */
  smoke: SmokeResult | Record<string, string> | null;
  /** 信任闭环 P0：本 run 的候选（含 evidence / expected_changes / gate）。 */
  candidates?: CandidateItem[];
  /** 本 run 相关版本链摘要（不含 snapshot）。 */
  versions?: SemanticVersion[];
  /** `(gate.decision, gate_adhoc)` 合成的有效结论。 */
  gate_effective?: string | null;
  /** 补 truth 后 ad-hoc 复跑结论（accept / reject / null）。 */
  gate_adhoc?: string | null;
  override?: OverrideInfo | null;
  /** 采纳后完整语料基准（异步：`run.bench_status` 推进后回填）。 */
  bench?: BenchResult | null;
  bench_status?: string | null;
  bench_mode?: string | null;
  rounds: unknown[];
  artifacts: Artifact[];
}

export interface EvolutionStatus {
  ok: boolean;
  repo_root: string;
  runs_dir: string;
  active_run: { run_id: string; stage: string } | null;
  note: string;
}

export interface LedgerTable {
  path: string;
  exists: boolean;
  mtime: string | null;
  items: Record<string, unknown>[];
  unreadable?: boolean;
}

export interface Ledgers {
  alias: LedgerTable;
  query_rule: LedgerTable;
  rejects: LedgerTable;
}

export interface FailureRow {
  created_at: string;
  question: string | null;
  reason_code: string | null;
  reason: string | null;
  sql: string | null;
  rejected_sql: string | null;
  resource_id: string | null;
}

export interface FailureList {
  rows: FailureRow[];
  since: string;
  until: string;
  total: number;
}

export interface DislikeRow {
  created_at: string;
  id: string;
  query: string | null;
  answer: string | null;
  feedback_reason: string | null;
  feedback_comment: string | null;
  adopted: boolean;
  kb_id: string | null;
  history_id: string | null;
  anchor: string | null;
  rounds: string | null;
  outcome: string | null;
  reason_code: string | null;
  sql: string | null;
}

export interface DislikeList {
  rows: DislikeRow[];
  since: string;
  until: string;
  total: number;
}

/** 点踩诊断·快照行（context.histories：该问句最近 5 条检索历史，正序旧→新）。 */
export interface DislikeHistorySnap {
  id: string;
  created_at: string | null;
  searched_at: string | null;
  /** 全文保真（不截断）。同一次问答可能存两条：本体层答案 / 最终合并答案。 */
  answer: string | null;
  answer_len: number | null;
  /** 该条就是反馈挂的锚（锚不在窗口内则该列表全 False）。 */
  is_feedback_target: boolean;
}

/** 点踩诊断·观测行 slim（context.observations；与桥接 `_obs_slim` 6 键口径对齐）。 */
export interface DislikeObservation {
  created_at: string | null;
  outcome: string | null;
  reason_code: string | null;
  /** 已截 300（桥接层口径）。 */
  sql: string | null;
  anchor: string | null;
  /** 多轮轮数（恒 str，对齐 /dislikes 口径）；无值为 null。 */
  rounds: string | null;
}

/** 试跑新落观测行 = 现场 6 键 + `row_count`/`data_source_id` + `reason`/`attribution`
 * （桥接 `_replay_obs_slim` 10 键；success 行 `attribution` 为 null）。 */
export interface ReplayObservation extends DislikeObservation {
  row_count: number | null;
  data_source_id: string | null;
  reason: string | null;
  attribution: ReplayAttribution | null;
}

/** 点踩诊断·现场段（`GET /evolution/dislikes/{id}/context`）。 */
export interface DislikeContext {
  /** 9 键窄形状（不是 DislikeRow——无 anchor/rounds/outcome/reason_code/sql，别混用）。 */
  feedback: {
    created_at: string | null;
    id: string;
    history_id: string | null;
    query: string | null;
    /** 截 200（桥接层口径；观测形态在 observations 段）。 */
    answer: string | null;
    feedback_reason: string | null;
    feedback_comment: string | null;
    adopted: boolean;
    kb_id: string | null;
  };
  histories: DislikeHistorySnap[];
  observations: DislikeObservation[];
}

/** 归因档（顶部与逐行同形）。 */
export interface ReplayAttribution {
  /** none / C / S / T / unknown。 */
  attribution: string;
  /** answered_ok / empty_result / no_observation / 失败细分 kind / unknown_code。 */
  kind: string;
  sub_key: string | null;
  /** 人话行动文案（后端原样输出，UI 直接展示、别改写）。 */
  action: string;
}

/** 点踩诊断·试跑 + 归因（`POST /evolution/dislikes/{id}/replay`）。 */
export interface ReplayResult {
  replay: {
    /** 任一新鲜观测行 success 且 row_count>0；无新鲜行 → null（未判定，零行 ≠ 答上）。 */
    answered: boolean | null;
    source: string | null;
    /** 截 300（桥接层口径）。 */
    answer_snippet: string;
    data_blocks: unknown[] | null;
    /** 本次试跑新落的**全部**观测行（升序；多源扇出可多行，不是「最新一条」）。 */
    new_observations: ReplayObservation[];
  };
  attribution: ReplayAttribution;
}

/** 失败样本的「现场摘要」（后端 diagnosis._scene_digest 输出；无现场样本不带此键）。 */
export interface SceneDigest {
  multiturn?: boolean;
  anchor?: string;
  tools?: string[];
  misses?: string[];
  near_miss?: string[];
  truncated?: boolean;
}

export interface DiagnosisProposal {
  id: string;
  attribution?: string;
  sub_key?: string;
  data_source_id?: string;
  count?: number;
  action?: string;
  samples?: { question?: string; sql?: string; reason?: string; scene?: SceneDigest }[];
}

export interface DiagnosisReport {
  window?: { since?: string; until?: string; total?: number; skipped?: number };
  total?: number;
  buckets?: { C?: number; S?: number; T?: number; unknown?: number };
  proposals?: DiagnosisProposal[];
  rejected_proposals?: DiagnosisProposal[];
}

export interface DiagnosisLatest {
  report: DiagnosisReport | null;
  json_path: string | null;
  md_path: string | null;
}

export interface CorpusCase {
  name: string;
  question: string;
  tier: string;
  expect: string;
  /** 以下为列表接口实际返回的字段（旧类型只声明了展示四列）。 */
  id?: string;
  kb_id?: string;
  ds_id?: string;
  enabled?: boolean;
}

/** `POST /corpus-cases`：expect=answer 必须带非空 control（后端 1001）。 */
export interface CorpusCaseBody {
  name: string;
  question: string;
  expect: 'answer' | 'refuse';
  kb_id: string;
  ds_id: string;
  tier?: string;
  control?: string[] | null;
  enabled?: boolean;
}

export interface CorpusCases {
  target: Record<string, unknown>;
  cases: CorpusCase[];
}

// ── 语料治理（Task 8）：覆盖率 + 按 Schema 播种 ──
/** `GET /corpus-coverage` 的一条 (kb, ds) 链。
 * `freshness` 与 `corpus_versions.record` 同口径：`missing`（无 active 版本）/
 * `fresh`（active hash == 当前语料快照）/ `drifted`（题改过但版本没入账）。 */
export interface EvolutionCorpusCoverageItem {
  kb_id: string;
  ds_id: string;
  /** 含停用题。 */
  total: number;
  /** 只计启用题；`by_tier` / `by_expect` 同此口径。 */
  enabled: number;
  by_tier: Record<string, number>;
  by_expect: Record<string, number>;
  active_version_id: string | null;
  active_hash: string | null;
  freshness: 'fresh' | 'drifted' | 'missing' | string;
  updated_at: string | null;
}

export interface EvolutionCorpusCoverage {
  items: EvolutionCorpusCoverageItem[];
  totals: { chains: number; total: number; enabled: number };
}

/** `POST /corpus-seed`：`mode="schema"` 从该源编译 schema 派生题；`cases` 只在
 * `mode="cases"` 时读取；`dry_run=true` 只算不写。 */
export interface CorpusSeedBody {
  kb: string;
  ds: string;
  mode: 'schema' | 'cases';
  cases?: unknown[];
  dry_run?: boolean;
}

export interface CorpusSeedResult {
  created: number;
  skipped: number;
  duplicates: number;
  conflicts: number;
  dry_run: boolean;
  mode: string;
  source: string;
  warnings: string[];
  items: Array<Record<string, unknown>>;
}

export interface StartRunBody {
  /** 幂等键（UUID）：同一次提交的重试复用它，桥接不再重复 spawn（T3-1）。 */
  request_id?: string;
  kind: EvolutionKind;
  kb?: string;
  ds?: string;
  repeat?: number;
  picks?: string[];
  target_cases?: string[];
  dry_run?: boolean;
  skip_probe?: boolean;
  window_days?: number;
  margin?: number;
  /** constraint / mapping / tool_schema 批的逐条操作（对应 kind 时必填；其它 kind 不送）。
   * relation 批只在勾选现有边时送 `[{op:"remove", name}]`；一条都不送 = 发现器模式。
   * mapping / tool_schema 是 KB 级 run：后端会拒绝同时携带 `ds`。 */
  entries?: ConstraintEntry[] | MappingEntry[] | RelationEntry[] | ToolSchemaEntry[];
}

/** CLI 子进程的返回壳（ok=false 是业务性失败，照常渲染 stdout 尾部）。 */
export interface CliResult {
  ok: boolean;
  rc: number;
  stdout_tail?: string;
  stderr_tail?: string;
  timeout?: boolean;
}

export interface AdoptPreview {
  planned: Record<string, unknown>[];
  skipped: SkipItem[];
  backup_hint: string;
  already_adopted: boolean;
  /** 门禁现状（预览不校验，只摊开）。 */
  gate?: GateDetail | null;
  gate_effective?: string | null;
  override_required?: boolean;
  /** 补 truth 后 ad-hoc 复跑已判 reject：override 也放不出来。 */
  adhoc_veto?: boolean;
  /** 待补 truth 的 unverified case。 */
  adhoc_cases?: AdhocCase[];
  smoke_plan?: SmokePlan | null;
}

/** `confirm=true` 的采纳请求体（信任闭环 P0）。 */
export interface ConfirmAdoptBody {
  picks?: string[];
  note?: string;
  /** 幂等键：同一次「确认采纳」的重试必须复用。 */
  request_id?: string;
  override_reason?: string;
  adhoc_truths?: AdhocTruth[];
  /** 人工 ad-hoc 判定（alias 批）：与 `adhoc_truths` **互斥** —— alias run 送
   * verdicts、其余 kind 继续送 truths，两者同时出现服务端 1001。 */
  adhoc_verdicts?: AdhocVerdict[];
}

/** 采纳响应：同步完成（200）与异步受理（202）共用一个形状。
 * 202 只带受理信息（`accepted=true` / `status=adopting` / `bench_status=pending`），
 * 没有 `run` / `patch` / `smoke` —— 前端必须轮询 run 详情。 */
export interface AdoptResult extends CliResult {
  run?: RunBundle['run'];
  patch?: PatchInfo | null;
  smoke?: SmokeResult | Record<string, string> | null;
  /** 本次请求被受理、后台执行中（HTTP 202）。 */
  accepted?: boolean;
  /** 受理/失败重放时的 run 状态（如 `adopting` / `smoke-failed`）。 */
  status?: string;
  /** 本次是否真的完成采纳（只有它为真才报「采纳完成」）。 */
  adopted?: boolean;
  bench_status?: string | null;
  bench?: BenchResult | null;
  version_id?: string | null;
  gate?: GateDetail | null;
  gate_effective?: string | null;
  gate_adhoc?: string | null;
  override?: OverrideInfo | null;
  idempotent_replay?: boolean;
  /** 采纳成功但旁路失败（台账/审计/归档等）—— 不改变采纳结论，但必须可见。 */
  warnings?: string[];
}

export interface RunActionResult extends CliResult {
  run: RunBundle['run'];
  patch?: PatchInfo | null;
}

/** 批量采纳请求（P1）：同 KB、每链一个 run、1..10 条。
 * **批量即确认** —— 没有 confirm 参数，传了（哪怕 false）服务端一律 1001。 */
export interface BatchAdoptBody {
  run_ids: string[];
  override_reason?: string;
}

/** 批量采纳的逐条结果：单条受理失败不影响其余（成功/失败都带 run_id）。 */
export interface BatchAdoptResultItem {
  run_id: string;
  accepted: boolean;
  /** 受理后的 run 状态（如 `adopting`）；单条失败时为 null。 */
  status: string | null;
  /** 单条失败的错误码（1001/1002/1003/3006…）；意外异常为 null。 */
  code: number | null;
  message: string | null;
  /** 命中了派生 request_id 的幂等登记（后台已在跑，没有重复受理）。 */
  idempotent_replay?: boolean;
}

/** `POST /runs/batch-adopt` 响应（data 段）：`accepted` / `rejected` 是计数。 */
export interface BatchAdoptResult {
  batch_id: string;
  accepted: number;
  rejected: number;
  results: BatchAdoptResultItem[];
}

/** 回滚响应。旧补丁逆操作路径**没有**版本那组键 → 全部 optional，前端按存在渲染。 */
export interface RollbackResult extends RunActionResult {
  version_id?: string | null;
  target_version_id?: string | null;
  legacy?: boolean;
  archived?: boolean;
  rolled_back?: number;
  count?: number;
  warnings?: string[];
  idempotent_replay?: boolean;
}

/** `POST /runs/{id}/entries/adjust`：CLI `adjust` 成功时空壳里并入的修订产物。 */
export interface AdjustResult extends CliResult {
  revised?: string;
  rationale?: string;
}

/** 条目人工调整 · 对话内容（messages 逐轮回放给模型）。 */
export interface AdjustRequest {
  entry_key: string;
  messages: { role: 'user' | 'assistant'; content: string }[];
}

export interface DiagnosisResult extends CliResult {
  since: string;
  report: DiagnosisReport | null;
  json_path: string | null;
  md_path: string | null;
}

/** `GET /metric-options`：该 KB 抽象 schema 的指标名单（`{name, additivity}`）。 */
export interface MetricOption {
  name: string;
  additivity: string;
}

/** `POST /failures/adopt-metric`：CLI 子进程返回壳（`ok=false` 是业务性失败，照常渲染 stdout 尾部）。 */
export interface MetricAdoptResult {
  ok: boolean;
  rc: number;
  stdout_tail?: string;
  stderr_tail?: string;
  [k: string]: unknown;
}

// ── 长操作按请求放宽超时（共享默认 30s 不够；spec 6.3）──
const TIMEOUT_START_RUN = 60_000;
const TIMEOUT_REJECT_ROLLBACK = 120_000;
const TIMEOUT_DIAGNOSIS = 300_000;
const TIMEOUT_ADOPT = 600_000; // 指标/别名采纳仍走同步写库链路（服务端写 schema + 回读），最坏几分钟
const TIMEOUT_ADOPT_ACCEPT = 60_000; // run 采纳是异步受理（202）立即返回；后台基准改由详情轮询推进
const TIMEOUT_BATCH_ADOPT = 120_000; // 最多 10 条串行受理，逐条带状态机校验；60s 对满批偏紧
const TIMEOUT_ADJUST = 320_000; // 一轮 LLM 修订（服务端链路）：前端预算先生效，网关 610s 只保证不静默截断在前
const TIMEOUT_EDIT = 120_000; // 写锁下写回（服务端链路）：前端预算先生效，网关 610s 只保证不静默截断在前
const TIMEOUT_REPLAY = 320_000; // 试跑一轮真实问答（含模型调用）：让服务端试跑口径先生效，而不是浏览器先判死（同 TIMEOUT_ADJUST 纪律）
const TIMEOUT_ADHOC_EVIDENCE = 320_000; // 验真取数 = 临时 apply + 一轮真实问答 + 恢复快照，与 replay 同档

// ── 服务端演化 API（runs / 动作 / ledgers / 诊断 / 点踩）：基址 `/api/v1/knowledge-base/evolution`。
// ②b Task 4 起全部函数走这里，不再有本地桥接客户端。──
const evolutionClient = {
  get: <T,>(url: string, cfg?: AxiosRequestConfig) =>
    apiClient.get<T>(`/knowledge-base/evolution${url}`, cfg),
  post: <T,>(url: string, body?: unknown, cfg?: AxiosRequestConfig) =>
    apiClient.post<T>(`/knowledge-base/evolution${url}`, body, cfg),
  patch: <T,>(url: string, body?: unknown, cfg?: AxiosRequestConfig) =>
    apiClient.patch<T>(`/knowledge-base/evolution${url}`, body, cfg),
  delete: <T,>(url: string, cfg?: AxiosRequestConfig) =>
    apiClient.delete<T>(`/knowledge-base/evolution${url}`, cfg),
};

export const getEvolutionStatus = () => evolutionClient.get<EvolutionStatus>('/status');

export const listEvolutionRuns = () =>
  evolutionClient.get<{ runs: EvolutionRunSummary[] }>('/runs');

export const getRunBundle = (runId: string) =>
  evolutionClient.get<RunBundle>(`/runs/${runId}`);

export const getRunLog = (runId: string) =>
  evolutionClient.get<{ lines: string[]; exists: boolean }>(`/runs/${runId}/log`);

/** `reused=true` = 本次提交命中了 request_id 幂等登记（重试复看既定 run，未新建）。 */
export const startEvolutionRun = (body: StartRunBody) =>
  evolutionClient.post<{ run_id: string; reused?: boolean }>('/runs', body,
                                                             { timeout: TIMEOUT_START_RUN });

export const previewAdopt = (runId: string, body: { picks?: string[] } = {}) =>
  evolutionClient.post<AdoptPreview>(`/runs/${runId}/adopt`, { ...body, confirm: false });

/** 人工 ad-hoc 验真取数：临时 apply 候选（按 `picks`）→ 跑一次问句 → 恢复快照。
 * 只读（不改 run 状态、不写台账）；会真实调用模型，与 replay 同档放宽超时。 */
export const fetchAdhocEvidence = (
  runId: string,
  caseId: string,
  picks: string[] = [],
) =>
  evolutionClient.post<AdhocEvidence>(
    `/runs/${runId}/adhoc-evidence`,
    { case_id: caseId, picks },
    { timeout: TIMEOUT_ADHOC_EVIDENCE },
  );

export const confirmAdopt = (runId: string, body: ConfirmAdoptBody = {}) =>
  evolutionClient.post<AdoptResult>(
    `/runs/${runId}/adopt`,
    { ...body, confirm: true },
    { timeout: TIMEOUT_ADOPT_ACCEPT },
  );

/** 提升 ad-hoc 语料：把本 run 已 verified 的 ad-hoc case 写入语料库。
 * `caseIds` 省略 = 由后端决定（当前实现按已 verified 全量）；前端总是传勾选集合。 */
export const promoteAdhoc = (runId: string, caseIds?: string[]) =>
  evolutionClient.post<PromoteAdhocResult>(
    `/runs/${runId}/promote-adhoc`,
    // 后端把空数组当 1001（「静默当成全部」是反义），空选择必须整字段省略。
    caseIds?.length ? { case_ids: caseIds } : {},
  );

/** 批量采纳（P1）：同 KB、每链一个 run、1..10 条，**调用即受理**（没有批量预览）。
 * 结构校验（同 KB / 同链 / 上限）服务端整体拒绝（1001，一条都不受理）；受理阶段
 * 单条失败只落进该条结果，整体 HTTP 200。后台按输入顺序串行执行。 */
export const batchAdoptRuns = (body: BatchAdoptBody) =>
  evolutionClient.post<BatchAdoptResult>(
    '/runs/batch-adopt',
    body,
    { timeout: TIMEOUT_BATCH_ADOPT },
  );

export const rejectRun = (runId: string, note: string) => evolutionClient.post<RunActionResult>(
    `/runs/${runId}/reject`,
    { note },
    { timeout: TIMEOUT_REJECT_ROLLBACK },
  );

/** 回滚：`to_version_id` 省略 → 后端回父版本（legacy run 会 409，需显式指定）。 */
export const rollbackRun = (
  runId: string,
  body: { to_version_id?: string; reason?: string } = {},
) =>
  evolutionClient.post<RollbackResult>(
    `/runs/${runId}/rollback`,
    body,
    { timeout: TIMEOUT_REJECT_ROLLBACK },
  );

/** 语义版本列表参数（`GET /evolution/versions`）。 */
export interface VersionListParams {
  kb: string;
  scope?: string;
  ds?: string;
  include_archived?: boolean;
  limit?: number;
  offset?: number;
}

export interface VersionListResult {
  items: SemanticVersion[];
  limit?: number;
  offset?: number;
  include_archived?: boolean;
  scope?: string | null;
  ds?: string | null;
}

export const listVersions = (params: VersionListParams) =>
  evolutionClient.get<VersionListResult>('/versions', { params });

/** 单个版本详情：默认不含完整 snapshot。 */
export const getVersion = (
  versionId: string,
  opts: { include_snapshot?: boolean; include_archived?: boolean } = {},
) => evolutionClient.get<SemanticVersion>(`/versions/${versionId}`, { params: opts });

/** 条目人工调整 · 对话一轮（只读；`ok=false` 是业务性失败，照常渲染 stdout 尾巴）。 */
export const adjustEntry = (runId: string, body: AdjustRequest) =>
  evolutionClient.post<AdjustResult>(`/runs/${runId}/entries/adjust`, body,
                                     { timeout: TIMEOUT_ADJUST });

/** 条目人工调整 · 写回 patch.json（恰给 alias 或 expression 之一）。 */
export const editEntry = (
  runId: string,
  body: { entry_key: string; alias?: string; expression?: string },
) => evolutionClient.post<RunActionResult>(`/runs/${runId}/entries/edit`, body,
                                           { timeout: TIMEOUT_EDIT });

export const getLedgers = () => evolutionClient.get<Ledgers>('/ledgers');

export const listFailures = (days = 7) =>
  evolutionClient.get<FailureList>('/failures', { params: { days } });

export const getDislikes = (days = 30) =>
  evolutionClient.get<DislikeList>('/dislikes', { params: { days } });

/** 点踩诊断·现场段（只读聚合）：反馈行 + 该问句最近 5 条快照时间线（正序）+ 最近 20 条观测。 */
export const getDislikeContext = (feedbackId: string) =>
  evolutionClient.get<DislikeContext>(`/dislikes/${feedbackId}/context`);

/** 点踩诊断·试跑：以该问句重走真实链路（router ② 主答 / DI 扇出），看现在会怎么答。
 * 会真实调用模型、并落观测行；空 body（服务端不需要 payload，用户身份走 Authorization 头）。 */
export const replayDislike = (feedbackId: string) =>
  evolutionClient.post<ReplayResult>(
    `/dislikes/${feedbackId}/replay`,
    {},
    { timeout: TIMEOUT_REPLAY },
  );

export const runDiagnosis = (windowDays = 7) =>
  evolutionClient.post<DiagnosisResult>(
    '/diagnosis',
    { window_days: windowDays },
    { timeout: TIMEOUT_DIAGNOSIS },
  );

export const getLatestDiagnosis = () => evolutionClient.get<DiagnosisLatest>('/diagnosis/latest');

export const listCorpusCases = () => evolutionClient.get<CorpusCases>('/corpus-cases');

export const createCorpusCase = (body: CorpusCaseBody) =>
  evolutionClient.post<{ id: string }>('/corpus-cases', body);

export const updateCorpusCase = (caseId: string, body: Partial<CorpusCaseBody>) =>
  evolutionClient.patch<{ id: string }>(`/corpus-cases/${caseId}`, body);

export const deleteCorpusCase = (caseId: string) =>
  evolutionClient.delete<{ id: string }>(`/corpus-cases/${caseId}`);

/** 语料治理 Task 8：按 (kb, ds) 的语料覆盖率（只读；仅租户管理员）。 */
export const getEvolutionCorpusCoverage = () =>
  evolutionClient.get<EvolutionCorpusCoverage>('/corpus-coverage');

/** 语料治理 Task 8：按编译 schema 播种 `COUNT(*)` 题（幂等去重、绝不覆盖冲突）。 */
export const seedEvolutionCorpus = (body: CorpusSeedBody) =>
  evolutionClient.post<CorpusSeedResult>('/corpus-seed', body);

/** 当前 KB × DS 的编译约束清单（constraint 批的可编辑底稿）。
 * 直接读 knowledge-base 的 compiled-schema 读口（先例 nl2sqlKbOptions.ts，不经演化桥接）。 */
export const getCompiledConstraints = (kb: string, ds: string) =>
  apiClient.get<{ constraints?: CompiledConstraint[] }>(
    '/knowledge-base/ontology/compiled-schema',
    { params: { knowledge_base_id: kb, data_source_id: ds } },
  );

// ── mapping 底稿：抽象 schema 的落点（KB 级） + 编译 schema 的列白名单 ──

/** 实体级落点（`included=false` = 对齐保留但不生效，mapping add 不可依附）。 */
export interface AbstractEntityLanding {
  dataSourceId: string;
  entityName: string;
  included?: boolean;
  stale?: boolean;
}

/** 属性级落点（承载 mapping 的四个语义槽；`enabled=false` = 停用）。 */
export interface AbstractAttributeLanding {
  dataSourceId: string;
  entityName: string;
  attributeName: string;
  role?: string;
  filter?: string;
  aggregation?: string;
  grain?: string;
  enabled?: boolean;
  stale?: boolean;
}

export interface AbstractAttribute {
  name: string;
  type?: string;
  primaryKey?: boolean;
  required?: boolean;
  sources?: AbstractAttributeLanding[];
}

export interface AbstractEntity {
  name: string;
  aliases?: string[];
  sources?: AbstractEntityLanding[];
  attributes?: AbstractAttribute[];
}

/** `GET /knowledge-base/ontology/abstract-schema` 的本地（camelCase）投影。 */
export interface AbstractSchemaPayload {
  schema?: {
    entities?: AbstractEntity[];
    relations?: unknown[];
  };
}

type RawRecord = Record<string, unknown>;
const asRecord = (value: unknown): RawRecord =>
  value && typeof value === 'object' ? (value as RawRecord) : {};
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const asText = (value: unknown): string => (value == null ? '' : String(value));

/** 服务端 snake_case → 本地 camelCase。槽只在有值时带（缺键不物化）。 */
function normalizeAbstractSchema(raw: unknown): AbstractSchemaPayload {
  const schema = asRecord(asRecord(raw).schema);
  const entities = asArray(schema.entities).map((item): AbstractEntity => {
    const entity = asRecord(item);
    return {
      name: asText(entity.name),
      aliases: asArray(entity.aliases).map(asText),
      sources: asArray(entity.sources).map((landing): AbstractEntityLanding => {
        const src = asRecord(landing);
        return {
          dataSourceId: asText(src.data_source_id),
          entityName: asText(src.entity_name),
          included: src.included !== false,
          stale: Boolean(src.stale),
        };
      }),
      attributes: asArray(entity.attributes).map((item2): AbstractAttribute => {
        const attr = asRecord(item2);
        return {
          name: asText(attr.name),
          type: attr.type == null ? undefined : asText(attr.type),
          primaryKey: Boolean(attr.primary_key),
          required: Boolean(attr.required),
          sources: asArray(attr.sources).map((landing): AbstractAttributeLanding => {
            const src = asRecord(landing);
            return {
              dataSourceId: asText(src.data_source_id),
              entityName: asText(src.entity_name),
              attributeName: asText(src.attribute_name),
              ...(src.role ? { role: asText(src.role) } : {}),
              ...(src.filter ? { filter: asText(src.filter) } : {}),
              ...(src.aggregation ? { aggregation: asText(src.aggregation) } : {}),
              ...(src.grain ? { grain: asText(src.grain) } : {}),
              ...(typeof src.enabled === 'boolean' ? { enabled: src.enabled } : {}),
              stale: Boolean(src.stale),
            };
          }),
        };
      }),
    };
  });
  return { schema: { entities, relations: asArray(schema.relations) } };
}

/** 抽象 schema（mapping 的行来源）。落点按当前 ds 过滤后展示。 */
export const getAbstractSchema = async (kb: string): Promise<AbstractSchemaPayload> =>
  normalizeAbstractSchema(await apiClient.get<unknown>(
    '/knowledge-base/ontology/abstract-schema',
    { params: { knowledge_base_id: kb } },
  ));

/** 编译 schema 的属性行：`name` 是落库列名（slug），`physical_name` 是物理列名。 */
export interface CompiledSchemaAttribute {
  name: string;
  physical_name?: string | null;
  type?: string | null;
}

export interface CompiledSchemaEntityType {
  name: string;
  attributes?: CompiledSchemaAttribute[];
}

/** 编译 schema 的 join 边行（relation 批「现有边」的来源）。
 * 端点与边名是删除的定位信息；origin/cardinality 只做展示。 */
export interface CompiledRelationType {
  name: string;
  source?: string | null;
  source_column?: string | null;
  target?: string | null;
  target_column?: string | null;
  origin?: string | null;
  cardinality?: string | null;
  description?: string | null;
}

/** 编译 schema 全行（mapping add 的列来源 + relation 的现有边）。 */
export interface CompiledSchemaRow {
  entity_types?: CompiledSchemaEntityType[];
  relation_types?: CompiledRelationType[];
  [key: string]: unknown;
}

export const getCompiledSchema = (kb: string, ds: string) =>
  apiClient.get<CompiledSchemaRow>(
    '/knowledge-base/ontology/compiled-schema',
    { params: { knowledge_base_id: kb, data_source_id: ds } },
  );

// ── 指标：采纳为指标别名——服务端写抽象 schema + 台账（含登录+回读），可能较慢 ──

export const getMetricOptions = (kb: string) =>
  evolutionClient.get<{ metrics: MetricOption[] }>('/metric-options', { params: { kb } });

export const adoptMetricAlias = (body: { id: string; metric: string; alias: string; kb: string }) =>
  evolutionClient.post<MetricAdoptResult>('/failures/adopt-metric', body, { timeout: TIMEOUT_ADOPT });

// ── 概念别名采纳（点踩诊断抽屉）：镜像指标版 ──
/** `GET /concept-options`：该 KB 抽象 schema 的概念名单（`{name, aliases}`）。 */
export interface ConceptOption {
  name: string;
  aliases: string[];
  /** 是否有可用落点（没有落点的概念采纳别名不会产生候选，来自 `/concept-options`）。 */
  has_landing?: boolean;
}

/** `POST /dislikes/{id}/adopt-alias`：CLI 子进程返回壳 + 反馈 adopted 标记结果。 */
export interface ConceptAdoptResult extends MetricAdoptResult {
  feedback_adopted?: boolean;
}

export const getConceptOptions = (kb: string) =>
  evolutionClient.get<{ concepts: ConceptOption[] }>('/concept-options', { params: { kb } });

// ── 多轮工具口径（Tool-Schema 线）：代码默认 ⊕ active 抽象行 `tools` 覆盖 ──

/** `GET /tool-options` 的单行：
 * - `description` = 代码默认（`TOOL_SCHEMAS` 的 function.description）；
 * - `effective_description` = 覆盖描述（非空且 active）否则代码默认；
 * - `enabled` = 覆盖条目显式 `enabled:false` 时为 false（其余一律 true）；
 * - `overridden` = 有效状态与代码默认不同（描述变了或被停用）。 */
export interface EvolutionToolOption {
  name: string;
  description: string;
  effective_description: string;
  enabled: boolean;
  overridden: boolean;
}

/** `source` = `abstract`（active 行有至少一条有效覆盖）/ `code`（否则）。 */
export interface EvolutionToolOptions {
  tools: EvolutionToolOption[];
  source: 'code' | 'abstract' | string;
}

export const listEvolutionToolOptions = (kb: string) =>
  evolutionClient.get<EvolutionToolOptions>('/tool-options', { params: { kb } });

export const adoptConceptAlias = (feedbackId: string, body: { concept: string; alias: string }) =>
  evolutionClient.post<ConceptAdoptResult>(
    `/dislikes/${feedbackId}/adopt-alias`, body, { timeout: TIMEOUT_ADOPT });
