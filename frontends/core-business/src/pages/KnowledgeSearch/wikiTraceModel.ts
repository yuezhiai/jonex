/**
 * [jonex] jonex_wiki 检索轨迹归一化（纯函数，便于单测）。
 *
 * 同一条链路的两个数据源必须收成**同一个形状**，否则「流式看到的」和
 * 「从检索历史回放的」会长得不一样：
 *
 *   1. `events` —— 引擎公开进度事件（流式轮询拿到；本次检索结束时后端把它
 *      写进 `reasoning.steps[].detail.trace_events` 落库，历史回放读的是同一份）；
 *   2. `calls` —— 后端从引擎公开 trace 投影的调用序列（含命中/读取计数、
 *      是否命中缓存），只在检索完成后才有。
 *
 * 事件给「输入 + 片段预览 + 耗时」，calls 给「精确计数」。两者都有时按工具
 * 顺序对齐合并（`enrichWithCalls`），只有一个时退化为那个。
 */
import type { WikiProgressEvent } from '@/types/knowledgeSearch';

export type WikiTraceKind = 'search' | 'read' | 'table' | 'plan' | 'synthesis';

export interface WikiTracePreview {
  title?: string;
  path?: string;
  excerpt?: string;
}

export interface WikiTraceStep {
  /** 稳定 key：同一库内由 tool + seq 组成，跨库不会撞 */
  id: string;
  seq: number;
  kbId?: string;
  kind: WikiTraceKind;
  tool?: string;
  /** 有 duration_ms（引擎完成事件）或来自 calls 的即视为已完成 */
  done: boolean;
  durationMs?: number;
  /** 起始时刻（epoch ms）：运行中的步骤用它算自己的耗时，而不是整次检索的总时长 */
  startedAtMs?: number;
  /** 步骤输入 */
  query?: string;
  tableId?: string;
  operation?: string;
  range?: [number, number];
  /** table_locate 的字面检索词、目标字段 */
  literal?: string;
  fieldId?: string;
  /** table_read 的行窗口（引擎从 scope.row_start/row_end 摊平出来） */
  rows?: [number, number];
  /** 步骤输出 */
  paths: string[];
  previews: WikiTracePreview[];
  /** 引擎完成事件自带的计数摘要："12 hit(s)" / "status=complete; cells=24" */
  resultSummary?: string;
  /** 引擎声明的命中/读取条数（calls 才有） */
  count?: number;
  cells?: number;
  matched?: number;
  hasMore?: boolean;
  status?: string;
  /** wiki_read 的 result_summary 会带 success=False（路径不存在/内容无效） */
  succeeded?: boolean;
  cached: boolean;
  /** 引擎原始 title（英文），动作名缺失时兜底展示 */
  title?: string;
}

/** 只有这三种事件算「链路步骤」，其余是心跳/控制事件 */
const STEP_TYPES = new Set(['tool', 'model', 'synthesis']);

function kindOf(tool: string, type?: string): WikiTraceKind {
  if (tool === 'image_find') return 'search';
  if (tool.startsWith('table_')) return 'table';
  if (tool.includes('search')) return 'search';
  if (tool) return 'read';
  if (type === 'synthesis') return 'synthesis';
  return 'plan';
}

function startedAtOf(value: unknown): number | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : undefined;
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item !== '') : [];
}

function asRange(value: unknown): [number, number] | undefined {
  if (Array.isArray(value) && value.length === 2 && value.every((n) => typeof n === 'number')) {
    return [value[0], value[1]];
  }
  return undefined;
}

/**
 * 引擎 `result_summary` → 结构化计数。
 *
 * 引擎只发两种采样：`"12 hit(s)"`（检索）和 `"key=value; key=value"`（读取/表格）。
 * 这里做容错解析，未知键直接忽略——它只是展示用，解析失败不该影响任何逻辑。
 */
export interface WikiSummary {
  hit?: number;
  files?: number;
  cells?: number;
  matched?: number;
  hasMore?: boolean;
  status?: string;
  success?: boolean;
}

export function parseResultSummary(summary?: string): WikiSummary {
  const out: WikiSummary = {};
  if (!summary) return out;
  const hit = /(\d+)\s*hit\(s\)/i.exec(summary);
  if (hit) out.hit = Number(hit[1]);
  for (const part of summary.split(';')) {
    const at = part.indexOf('=');
    if (at < 0) continue;
    const key = part.slice(0, at).trim().toLowerCase();
    const value = part.slice(at + 1).trim();
    if (!key || !value) continue;
    const num = Number(value);
    if (key === 'files' && Number.isFinite(num)) out.files = num;
    else if (key === 'cells' && Number.isFinite(num)) out.cells = num;
    else if (key === 'matched' && Number.isFinite(num)) out.matched = num;
    else if (key === 'has_more') out.hasMore = value.toLowerCase() === 'true';
    else if (key === 'status') out.status = value;
    else if (key === 'success') out.success = value.toLowerCase() === 'true';
  }
  return out;
}

function previewsOf(value: unknown): WikiTracePreview[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map((item) => ({
      title: typeof item.title === 'string' ? item.title : undefined,
      path: typeof item.path === 'string' ? item.path : undefined,
      excerpt: typeof item.excerpt === 'string' ? item.excerpt : undefined,
    }))
    .filter((item) => Boolean(item.title || item.path || item.excerpt));
}

/** 进度事件 → 轨迹步骤：start/finish 两条合成一行（与后端成对事件一致）。 */
export function traceFromEvents(events: readonly WikiProgressEvent[]): WikiTraceStep[] {
  // 轮询把多个库的事件并进一个数组；库内 seq 才是权威顺序。
  const libraries = new Map<string, WikiProgressEvent[]>();
  for (const event of events) {
    if (!event.tool && !STEP_TYPES.has(event.type ?? '')) continue;
    const key = event.kb_id ?? '';
    const group = libraries.get(key) ?? [];
    group.push(event);
    libraries.set(key, group);
  }

  const steps: WikiTraceStep[] = [];
  const pending = new Map<string, number>();
  const ordered = [...libraries.values()].flatMap((group) => group.sort((a, b) => a.seq - b.seq));
  for (const event of ordered) {
    const tool = event.tool ?? '';
    const key = `${event.kb_id ?? ''}:${tool || event.type}`;
    const details = event.details ?? {};
    const finished = typeof event.duration_ms === 'number';
    const summary = parseResultSummary(typeof event.result_summary === 'string' ? event.result_summary : undefined);
    const step: WikiTraceStep = {
      id: `${key}:${event.seq}:${steps.length}`,
      seq: event.seq,
      kbId: event.kb_id,
      kind: kindOf(tool, event.type),
      tool: tool || undefined,
      done: finished,
      durationMs: finished ? (event.duration_ms as number) : undefined,
      startedAtMs: startedAtOf(event.started_at),
      query: typeof details.query === 'string' ? details.query : undefined,
      tableId: typeof details.table_id === 'string' ? details.table_id : undefined,
      operation: typeof details.operation === 'string' ? details.operation : undefined,
      literal: typeof details.literal === 'string' ? details.literal : undefined,
      fieldId: typeof details.field_id === 'string' ? details.field_id : undefined,
      range: asRange(details.range),
      rows: asRange(details.rows),
      paths: asStrings(details.paths),
      previews: previewsOf(details.previews),
      resultSummary: typeof event.result_summary === 'string' ? event.result_summary : undefined,
      count: summary.hit ?? summary.files,
      cells: summary.cells,
      matched: summary.matched,
      hasMore: summary.hasMore,
      status: summary.status,
      succeeded: summary.success,
      cached: false,
      title: event.title,
    };
    const index = pending.get(key);
    if (finished && index != null && !steps[index].done) {
      // 完成事件只带耗时与计数，输入/预览留在起始那行上，这里合并而不是覆盖。
      const start = steps[index];
      steps[index] = {
        ...start,
        done: true,
        durationMs: step.durationMs,
        startedAtMs: start.startedAtMs ?? step.startedAtMs,
        query: step.query ?? start.query,
        tableId: step.tableId ?? start.tableId,
        operation: step.operation ?? start.operation,
        literal: step.literal ?? start.literal,
        fieldId: step.fieldId ?? start.fieldId,
        range: step.range ?? start.range,
        rows: step.rows ?? start.rows,
        paths: step.paths.length ? step.paths : start.paths,
        previews: step.previews.length ? step.previews : start.previews,
        // 完成事件才带 result_summary：命中数/单元格数在流式阶段就靠它显示「返回」。
        resultSummary: step.resultSummary ?? start.resultSummary,
        count: summary.hit ?? summary.files ?? start.count,
        cells: summary.cells ?? start.cells,
        matched: summary.matched ?? start.matched,
        hasMore: summary.hasMore ?? start.hasMore,
        status: summary.status ?? start.status,
        succeeded: summary.success ?? start.succeeded,
      };
      pending.delete(key);
    } else {
      steps.push(step);
      if (!finished) pending.set(key, steps.length - 1);
    }
  }
  return steps;
}

/** 后端 `_jonex_wiki_chain_calls` 的条目（宽松类型，缺字段不炸） */
export interface WikiChainCall {
  tool?: string;
  query?: string;
  paths?: unknown;
  count?: unknown;
  cached?: unknown;
  table_id?: string;
  operation?: string;
  status?: string;
  cells?: unknown;
  has_more?: unknown;
  matched?: unknown;
}

function numberOf(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function booleanOf(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

/** 公开 trace 调用序列 → 轨迹步骤（老快照没有 events 时的回退路径）。 */
export function traceFromCalls(calls: readonly WikiChainCall[]): WikiTraceStep[] {
  return calls
    .filter((call) => typeof call?.tool === 'string' && call.tool !== '')
    .map((call, index) => {
      const tool = call.tool as string;
      return {
        id: `call:${tool}:${index}`,
        seq: index + 1,
        kind: kindOf(tool, 'tool'),
        tool,
        done: true,
        query: typeof call.query === 'string' && call.query ? call.query : undefined,
        tableId: typeof call.table_id === 'string' ? call.table_id : undefined,
        operation: typeof call.operation === 'string' ? call.operation : undefined,
        range: undefined,
        paths: asStrings(call.paths),
        previews: [],
        count: numberOf(call.count),
        cells: numberOf(call.cells),
        matched: numberOf(call.matched),
        hasMore: booleanOf(call.has_more),
        status: typeof call.status === 'string' ? call.status : undefined,
        cached: call.cached === true,
      };
    });
}

/**
 * 把 calls 的精确计数按「同工具、同顺序」贴回 events 生成的步骤上。
 *
 * 引擎进度事件只有「最多 3 个预览」，没有命中总数；calls 有 `count` 但没有片段。
 * 两边都按 wiki_search / wiki_read 的出现顺序排列，所以可以按下标对齐。
 */
export function enrichWithCalls(steps: WikiTraceStep[], calls: readonly WikiChainCall[]): WikiTraceStep[] {
  if (!calls.length || !steps.length) return steps;
  const byTool = new Map<string, WikiChainCall[]>();
  for (const call of calls) {
    const tool = typeof call?.tool === 'string' ? call.tool : '';
    if (!tool) continue;
    const bucket = byTool.get(tool) ?? [];
    bucket.push(call);
    byTool.set(tool, bucket);
  }
  const used = new Map<string, number>();
  return steps.map((step) => {
    const tool = step.tool ?? '';
    const bucket = byTool.get(tool);
    if (!bucket) return step;
    const index = used.get(tool) ?? 0;
    used.set(tool, index + 1);
    const call = bucket[index];
    if (!call) return step;
    return {
      ...step,
      count: numberOf(call.count) ?? step.count,
      cells: numberOf(call.cells) ?? step.cells,
      matched: numberOf(call.matched) ?? step.matched,
      hasMore: booleanOf(call.has_more) ?? step.hasMore,
      status: (typeof call.status === 'string' ? call.status : undefined) ?? step.status,
      operation: step.operation ?? (typeof call.operation === 'string' ? call.operation : undefined),
      cached: call.cached === true || step.cached,
      query: step.query ?? (typeof call.query === 'string' && call.query ? call.query : undefined),
      paths: step.paths.length ? step.paths : asStrings(call.paths),
    };
  });
}

/**
 * 一步的「返回」：只描述引擎真的声明过的东西，没有就返回 undefined
 * （运行中、模型步骤、被截断的事件都不该硬编一句结果出来）。
 */
export type WikiReturnKind = 'search' | 'read' | 'table' | 'compute';

export interface WikiReturn {
  kind: WikiReturnKind;
  /** 命中条数 / 读取页数 / 单元格数 */
  count?: number;
  /** table_compute / table_locate 的匹配数 */
  matched?: number;
  hasMore?: boolean;
  excerpts?: number;
  failed?: boolean;
}

export function returnOf(step: WikiTraceStep): WikiReturn | undefined {
  if (step.kind === 'search') {
    if (step.count == null) return undefined;
    return { kind: 'search', count: step.count, excerpts: step.previews.length || undefined };
  }
  if (step.kind === 'read') {
    const count = step.count ?? step.paths.length;
    if (!count) return undefined;
    return {
      kind: 'read',
      count,
      excerpts: step.previews.length || undefined,
      failed: step.succeeded === false,
    };
  }
  if (step.kind === 'table') {
    if (step.cells == null && step.matched == null) return undefined;
    return {
      kind: step.tool === 'table_compute' ? 'compute' : 'table',
      count: step.cells ?? undefined,
      matched: step.matched ?? undefined,
      hasMore: step.hasMore,
    };
  }
  return undefined;
}

export function buildWikiTrace(
  events: readonly WikiProgressEvent[] | undefined,
  calls: readonly WikiChainCall[] | undefined,
): WikiTraceStep[] {
  const fromEvents = events?.length ? traceFromEvents(events) : [];
  if (fromEvents.length) return enrichWithCalls(fromEvents, calls ?? []);
  return traceFromCalls(calls ?? []);
}

/** 轨迹里还没结束的那一步（用于头部「正在…」）。没有则是最后一步。 */
export function runningStep(steps: readonly WikiTraceStep[]): WikiTraceStep | undefined {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (!steps[i].done) return steps[i];
  }
  return undefined;
}

export function totalDurationMs(steps: readonly WikiTraceStep[]): number {
  return steps.reduce((sum, step) => sum + (step.durationMs ?? 0), 0);
}

// 耗时格式化只有一份实现：ReasoningCard.tsx 的 fmtTraceMs（两条链共用）。
// 这里原先的 formatDuration 与它逐字节等价，已删，避免两处口径漂移。
