/**
 * 调用记录「首屏 `-` 后自动补值」。
 *
 * ## 为什么不是「再拉一次列表」
 *
 * LLM 网关写计量是异步批量落库（`LLMGW_PG_FLUSH_MAX_SECONDS`，当前 1 秒），
 * 列表首屏返回时部分行还没有 Token。补值只需要这几个数字，重跑整个列表会把
 * 筛选展开、降级判定、总数统计全做一遍，还会让表格整块刷新。所以走轻量接口
 * `GET /platform/monitor/calls/token-usage`，只拿 `requestId → totalTokens`。
 *
 * ## 口径（2026-11-10 需求方确认）
 *
 * - 每 2 秒补一次，最多 5 次（10 秒）。flush 周期 1 秒，通常第二轮就补齐；
 * - 10 秒后仍没有值的行**保持 `-`**：接口无法区分「本来就没调模型」（如
 *   `list_documents`）和「计量没落库」，不能无限等；
 * - 全部行都拿到值就提前停，不再发请求；
 * - 补值失败静默吞掉（下一轮再试）—— 这是锦上添花，不该把页面打成错误态。
 *
 * 新的一次列表取数（翻页 / 改筛选 / 刷新）会取消上一轮并重新盯新的一页；
 * 已补到的值按 `requestId` 累积保留（这个数字只会从「没有」变成「有」，不会回退）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { getCallTokenUsage } from '../../api/platformMonitor';

/** 相邻两次补值的间隔（毫秒） */
export const TOKEN_BACKFILL_INTERVAL_MS = 2000;
/** 一次取数最多补几轮（含首轮）—— 与 `LLMGW_PG_FLUSH_MAX_SECONDS=1.0` 对齐 */
export const TOKEN_BACKFILL_MAX_ATTEMPTS = 5;

export interface CallTokenBackfill {
  /** 已经补到的值：`requestId → totalTokens`（缺失或不含该键 = 还没有值） */
  tokens: Record<string, number>;
  /** 盯一页缺值的行；传空数组即取消上一轮（这一页不需要补） */
  schedule: (requestIds: string[]) => void;
}

export function useCallTokenBackfill(): CallTokenBackfill {
  const [tokens, setTokens] = useState<Record<string, number>>({});
  // 每次 schedule 自增：异步回包与定时器都先校验代次，避免旧轮次把新一页的值按旧 id 写进来
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
    (requestIds: string[]) => {
      generationRef.current += 1;
      const generation = generationRef.current;
      cancelTimer();

      // 同一 `requestId` 在降级模式下会出现多行，去重后再请求
      const targets = Array.from(new Set(requestIds.filter(Boolean)));
      if (targets.length === 0) return;

      const resolved = new Set<string>();
      let attempts = 0;

      const run = async () => {
        attempts += 1;
        try {
          const pending = targets.filter((id) => !resolved.has(id));
          const items = await getCallTokenUsage(pending);
          if (generation !== generationRef.current) return;
          const patch: Record<string, number> = {};
          for (const item of items) {
            if (item.totalTokens == null) continue;
            patch[item.requestId] = item.totalTokens;
            resolved.add(item.requestId);
          }
          if (Object.keys(patch).length > 0) {
            setTokens((prev) => ({ ...prev, ...patch }));
          }
        } catch {
          // 静默：补值失败不改变页面状态，下一轮再试
        }
        if (generation !== generationRef.current) return;
        if (resolved.size >= targets.length || attempts >= TOKEN_BACKFILL_MAX_ATTEMPTS) return;
        timerRef.current = setTimeout(() => {
          void run();
        }, TOKEN_BACKFILL_INTERVAL_MS);
      };

      timerRef.current = setTimeout(() => {
        void run();
      }, TOKEN_BACKFILL_INTERVAL_MS);
    },
    [cancelTimer],
  );

  return { tokens, schedule };
}

export default useCallTokenBackfill;
