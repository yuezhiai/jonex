/**
 * 抽象 schema 落点 / 构造的**后台 run** 客户端（landing-agent 线）。
 *
 * 与同步 `generate` 的区别：提交只建单（毫秒级返回），真正的 LLM 聚类/落点在
 * 服务端后台跑；前端每 2 秒轮询 `status/phase/progress`，终态后自行重拉 schema。
 * 响应 snake_case → camelCase 在本地 `mapLandingRun` 里做（本仓没有 codegen）。
 */
import { getData, postData, request } from './request';
import type { ApiResponse } from './request';

export type LandingRunMode = 'full' | 'landings';

/** run 状态：queued → running → succeeded | failed（v1 没有 cancelled）。 */
export type LandingRunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

/** `LandingProgress` 的前端形状（计数口径见后端 runner）。 */
export interface LandingRunProgress {
  phase: string;
  /** 截断/过滤后实际参与的概念数（run 开始即确定）。 */
  conceptsTotal: number;
  /** 已处理完的概念数（含异常/放弃）。 */
  conceptsDone: number;
  landed: number;
  unresolved: number;
  lowConfidence: number;
  toolCalls: number;
  tokens: number;
}

export interface LandingRun {
  runId: string;
  status: LandingRunStatus;
  phase: string;
  progress: LandingRunProgress;
  summary?: Record<string, unknown>;
  error?: string | null;
  /** 终态摘要里带的 schema 版本（没有则不返回该键）。 */
  schemaVersion?: number | null;
}

export interface StartedLandingRun {
  runId: string;
  status: string;
}

/** 缺键安全回落：轮询是高频路径，任何字段缺失都不能抛。 */
export function mapLandingRun(raw: any): LandingRun {
  const progress = raw?.progress && typeof raw.progress === 'object' ? raw.progress : {};
  const status = raw?.status;
  return {
    runId: raw?.run_id ?? '',
    status:
      status === 'running' || status === 'succeeded' || status === 'failed'
        ? status
        : 'queued',
    phase: raw?.phase ?? progress.phase ?? '',
    progress: {
      phase: progress.phase ?? raw?.phase ?? '',
      conceptsTotal: progress.concepts_total ?? 0,
      conceptsDone: progress.concepts_done ?? 0,
      landed: progress.landed ?? 0,
      unresolved: progress.unresolved ?? 0,
      lowConfidence: progress.low_confidence ?? 0,
      toolCalls: progress.tool_calls ?? 0,
      tokens: progress.tokens ?? 0,
    },
    summary: raw?.summary && typeof raw.summary === 'object' ? raw.summary : {},
    error: typeof raw?.error === 'string' ? raw.error : raw?.error ?? null,
    ...(raw?.schema_version !== undefined ? { schemaVersion: raw.schema_version } : {}),
  };
}

export interface StartLandingRunOptions {
  /** 业务词表；只有 `full`（全部重来）会用它，`landings` 后端会忽略并回 warning。 */
  aliasHints?: string;
  /** 已有已确认（active）的抽象 schema 时，显式确认覆盖才带 true（否则 1003）。 */
  force?: boolean;
}

/** 受理一次后台生成（同 KB 在飞 → 429 / 3007；active 且非 force → 1003）。 */
export async function startLandingRun(
  kbId: string,
  runMode: LandingRunMode,
  options: StartLandingRunOptions = {},
): Promise<StartedLandingRun> {
  const body = await postData<any>('/knowledge-base/ontology/abstract-schema/landing-runs', {
    knowledge_base_id: kbId,
    run_mode: runMode,
    // 落点模式不重造概念/别名，词表在本模式没有作用域 —— 干脆不发，避免让
    // 后端在摘要里补一条「未生效」的 warning 干扰用户。
    ...(runMode === 'full' ? { alias_hints: options.aliasHints ?? '' } : {}),
    ...(options.force ? { force: true } : {}),
  });
  return {
    runId: body?.run_id ?? '',
    status: body?.status ?? '',
  };
}

/** 轮询一次 run（不带 schema 正文）。 */
export async function getLandingRun(kbId: string, runId: string): Promise<LandingRun> {
  const res = request.get<ApiResponse<any>>(
    `/knowledge-base/ontology/abstract-schema/landing-runs/${encodeURIComponent(runId)}`,
    { params: { knowledge_base_id: kbId } },
  );
  return mapLandingRun(await getData(res));
}
