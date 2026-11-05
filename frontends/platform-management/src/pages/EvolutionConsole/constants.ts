/** 演化台共用常量：stage 进度表 / 状态色 / kind 标签。
 *
 * stage 表与 evolve.py 的 save_run 写点顺序一致：
 * created → drafted → probed → parent-ran → applied → candidate-ran → rolled-back → gated
 */

export const STAGE_ORDER = [
  'created', 'drafted', 'probed', 'parent-ran', 'applied', 'candidate-ran', 'rolled-back', 'gated',
] as const;

/** 'failed@applied' → { base: 'applied', failed: true }；普通 stage 原样。 */
export function parseStage(stage: string): { base: string; failed: boolean } {
  if (stage.startsWith('failed@')) return { base: stage.slice('failed@'.length), failed: true };
  return { base: stage, failed: false };
}

/** Steps 的 current 下标（created=0 … gated=7）；未知回 0。 */
export function stageIndex(stage: string): number {
  const { base } = parseStage(stage);
  const i = (STAGE_ORDER as readonly string[]).indexOf(base);
  return i < 0 ? 0 : i;
}

/** run.status → antd Tag 颜色。 */
export function statusTagColor(status: string): string {
  if (status === 'evaluated' || status === 'adopted') return 'success';
  if (status === 'failed' || status === 'unreadable' || status === 'smoke-failed') return 'error';
  if (status === 'running' || status === 'adopting') return 'processing';
  if (status === 'dry-run') return 'cyan';
  return 'default';
}

/** run.stage/status 的展示文案 key（App 内翻译；未知原样显示）。 */
export function stageLabelKey(stage: string): string {
  return `evolutionConsole.stage.${stage}`;
}

export const EVOLUTION_TAB_KEYS = ['failures', 'runs', 'ledgers', 'dislikes', 'corpus'] as const;
export type EvolutionTabKey = (typeof EVOLUTION_TAB_KEYS)[number];

/** ?tab= 参数校验：非法/缺省一律回第一签（仿 DataOperations 的 toDoTabKey）。 */
export function toEvolutionTabKey(value: string | null): EvolutionTabKey {
  return EVOLUTION_TAB_KEYS.includes(value as EvolutionTabKey)
    ? (value as EvolutionTabKey)
    : EVOLUTION_TAB_KEYS[0];
}

/** 与 evolve.py cmd_rollback 的四个 rc=0 输出分支对齐（tests/eval/nl2sql/evolve.py 的 cmd_rollback；
 * 行号常漂，这里只按 stdout 字面判定）：
 * ①「本轮还没出过补丁，无需回滚」②「rollback.added 为空 …（no-op）」
 * ③「库中已无本批条目…未写库」（该支现在也判 false）——①②③ 都归 no-op（false）；
 * ④「已回滚 N 条；再跑一次是 no-op」（N≥1）判 done。
 * ②④ 含 "no-op"、②③ 含「已回滚过」：不能按 "no-op" 或「已回滚」子串判定，
 * 只能按 N≥1 的字面判定。 */
export const isRollbackDone = (stdout?: string) => /已回滚\s*[1-9]\d*\s*条/.test(stdout ?? '');

/** 处理跳转：platform-management 是 MemoryRouter，跨子应用必须走顶层 location
 * （KB 跟踪页在 core-business：/apps/core-business/domain-knowledge/<kb_id>?tab=tracking）。
 * 两个消费方（DislikesTab 的「去处理」与 DiagnosisDrawer 的归因跳转）都从这里取，
 * 别在组件间互相 import（先例 EntryAdjustModal：子组件不反向 import 宿主页）。 */
export const trackingUrl = (kbId: string) =>
  `/apps/core-business/domain-knowledge/${encodeURIComponent(kbId)}?tab=tracking`;
