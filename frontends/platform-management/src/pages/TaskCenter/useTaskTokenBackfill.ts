/**
 * 任务中心「首屏 `-` 后自动补 Token」。
 *
 * 结构与 `PlatformMonitor/useCallTokenBackfill.ts` 同源，五条不变量一字不差：
 *
 * 1. **首次 2 秒后才请求**（给网关 1 秒 / 5 秒的落库周期留余量）；
 * 2. 每 2 秒一轮、最多 5 轮（10 秒），全部有结果就提前停；
 * 3. 新的 `schedule` 取消上一轮（`generationRef` 代次防串页），卸载后不再 `setState`；
 * 4. 只补缺失值、累积合并 —— 后来的 `null` 不覆盖已知值；
 * 5. 请求失败静默吞掉，下一轮再试，不把页面打成错误态。
 *
 * 与调用记录那处的两点不同（按文档归集）：
 *
 * - 值不只一个数字，还带「来源文档」（任务 ↔ 文档 1:1）；
 * - **运行中任务每轮都重查**（Token 在任务推进中持续增长，不能拿到第一个值就钉住）；
 *   `document_id === null`（非知识库文档任务）则视为已解析，不再空转。
 *
 * 只拿 `/task-center/tasks/token-usage` 的轻量补值接口，不重跑列表。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { getTaskTokenUsage } from '../../api/taskCenter';

/** 相邻两次补值的间隔（毫秒） */
export const TASK_TOKEN_BACKFILL_INTERVAL_MS = 2000;
/** 一次调度最多补几轮（含首轮） */
export const TASK_TOKEN_BACKFILL_MAX_ATTEMPTS = 5;
/** 单次请求的任务数上限（后端 `_MAX_TOKEN_TASK_IDS = 100`，超出会被 1001 拒掉） */
export const TASK_TOKEN_BACKFILL_BATCH = 100;

export interface TaskTokenUsageValue {
  documentId: string | null;
  documentName: string | null;
  /** null = 算不出来 / 没有精确计量，**不是 0** */
  totalTokens: number | null;
  /** 解析阶段；`totalTokens` 为 null 时同为 null */
  parseTokens: number | null;
  /** 本体阶段（抽取 + 本体向量化） */
  ontologyTokens: number | null;
  /** Wiki 编译阶段中归到本文档的部分 */
  wikiTokens: number | null;
  /** Wiki 整库编译段平摊给本文档的一份（已计入 totalTokens） */
  kbCompileTokens: number | null;
  /** 这一整库编译批次覆盖的文档数（渲染「本批 N 份共享」） */
  batchSize: number | null;
}

export interface TaskTokenBackfill {
  /** 已补到的值：`taskId → 用量`（不含该键 = 还没有值） */
  usage: Record<string, TaskTokenUsageValue>;
  /**
   * 盯一批任务：`runningIds` 里的任务每轮都重查，其余拿到非空合计即固定。
   * 传空数组即取消上一轮。
   */
  schedule: (taskIds: string[], runningIds?: ReadonlySet<string>) => void;
}

export function useTaskTokenBackfill(): TaskTokenBackfill {
  const [usage, setUsage] = useState<Record<string, TaskTokenUsageValue>>({});
  const generationRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // 卸载时停表，并让在飞的请求回来后不再 setState
  useEffect(
    () => () => {
      generationRef.current += 1;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );

  const schedule = useCallback(
    (taskIds: string[], runningIds?: ReadonlySet<string>) => {
      generationRef.current += 1;
      const generation = generationRef.current;
      cancelTimer();

      const targets = Array.from(new Set(taskIds.filter(Boolean)));
      if (targets.length === 0) return;

      const running = runningIds ?? new Set<string>();
      const resolved = new Set<string>();
      let attempts = 0;

      const run = async () => {
        attempts += 1;
        const pending = targets.filter((id) => !resolved.has(id));
        if (pending.length === 0) return;

        try {
          const groups = await Promise.all(
            Array.from({ length: Math.ceil(pending.length / TASK_TOKEN_BACKFILL_BATCH) }, (_, i) =>
              getTaskTokenUsage(
                pending.slice(i * TASK_TOKEN_BACKFILL_BATCH, (i + 1) * TASK_TOKEN_BACKFILL_BATCH),
              ),
            ),
          );
          if (generation !== generationRef.current) return;

          const patch: Record<string, TaskTokenUsageValue> = {};
          for (const item of groups.flat()) {
            if (!pending.includes(item.task_id)) continue;
            patch[item.task_id] = {
              documentId: item.document_id,
              documentName: item.document_name,
              totalTokens: item.total_tokens,
              parseTokens: item.parse_tokens ?? null,
              ontologyTokens: item.ontology_tokens ?? null,
              wikiTokens: item.wiki_tokens ?? null,
              kbCompileTokens: item.kb_compile_tokens ?? null,
              batchSize: item.batch_size ?? null,
            };
            if (item.document_id === null) {
              // 非知识库文档任务：没有映射，再查也不会有 Token
              resolved.add(item.task_id);
            } else if (!running.has(item.task_id) && item.total_tokens != null) {
              // 终态任务拿到精确值即固定；运行中的留到后续轮次继续刷新
              resolved.add(item.task_id);
            }
          }
          if (Object.keys(patch).length > 0) {
            setUsage((prev) => {
              const next = { ...prev };
              for (const [taskId, value] of Object.entries(patch)) {
                const old = next[taskId];
                next[taskId] = {
                  documentId: value.documentId ?? old?.documentId ?? null,
                  documentName: value.documentName ?? old?.documentName ?? null,
                  // 后来的 null 不覆盖已知值
                  totalTokens: value.totalTokens ?? old?.totalTokens ?? null,
                  parseTokens: value.parseTokens ?? old?.parseTokens ?? null,
                  ontologyTokens: value.ontologyTokens ?? old?.ontologyTokens ?? null,
                  wikiTokens: value.wikiTokens ?? old?.wikiTokens ?? null,
                  kbCompileTokens: value.kbCompileTokens ?? old?.kbCompileTokens ?? null,
                  batchSize: value.batchSize ?? old?.batchSize ?? null,
                };
              }
              return next;
            });
          }
        } catch {
          // 静默：补值失败不改变页面状态，下一轮再试
        }
        if (generation !== generationRef.current) return;
        if (resolved.size >= targets.length || attempts >= TASK_TOKEN_BACKFILL_MAX_ATTEMPTS) return;
        timerRef.current = setTimeout(() => {
          void run();
        }, TASK_TOKEN_BACKFILL_INTERVAL_MS);
      };

      timerRef.current = setTimeout(() => {
        void run();
      }, TASK_TOKEN_BACKFILL_INTERVAL_MS);
    },
    [cancelTimer],
  );

  return { usage, schedule };
}

export default useTaskTokenBackfill;
