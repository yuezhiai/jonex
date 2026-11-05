/** 只有这些状态才证明 `bench_started_at` 属于本次尝试（白名单，不做「非 pending 即可信」）。
 * 未知/新状态一律回落到观察时刻：宁可把窗口算短，也不要把上一轮的时间戳当本轮起点。 */
const TRUSTED = new Set(['parent_ran', 'passed', 'failed', 'warn', 'skipped']);

/** adopting 计时起点（轮询退避与 30 分钟告警都按它算）。
 *
 * `run.bench_started_at` 只有在本次尝试真的开跑后才可信：重试复用的是同一行
 * run，`pending` 阶段该时间戳可能还是上一次尝试留下的。因此：
 * - 状态不在白名单（含 `pending` / 缺失）→ 用 UI 首次观察到 adopting 的时刻；
 * - `parent_ran` 及以后 且时间戳合法 → 用后端时间戳（硬刷新不重置窗口）；
 * - 时间戳非法 → 回落到观察时刻。
 */
export const resolveAdoptingStart = (
  benchStatus: string | null,
  benchStartedAt: number,
  observedAt: number,
): number => {
  const trustworthy = benchStatus !== null && TRUSTED.has(benchStatus);
  return trustworthy && Number.isFinite(benchStartedAt) ? benchStartedAt : observedAt;
};
