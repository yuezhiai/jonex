import { useEffect, useState } from 'react';
import { getMetaFilters } from '../../api/platformMonitor';
import type { PmMetaFilters } from './types';

/**
 * 进程内的 M1 筛选元数据缓存。
 *
 * 只缓存**成功**的结果：失败不写缓存，下次挂载会重试 —— 把失败也缓存住，
 * 「第一次拿不到、第二次就好了」的情况要刷新两遍才生效。在途请求同样复用，
 * 三个页签同时挂载时只打一次接口。
 */
let cached: PmMetaFilters | null = null;
let inflight: Promise<PmMetaFilters | null> | null = null;

function loadMeta(): Promise<PmMetaFilters | null> {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = getMetaFilters()
      .then((data) => {
        cached = data;
        return data;
      })
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export interface PmMetaState {
  meta: PmMetaFilters | null;
  /**
   * 拉取是否**失败**（已回退静态候选）。加载中不算失败 —— 两者都拿不到 meta，
   * 但只有失败才值得告诉用户「下拉候选可能不全」。
   */
  failed: boolean;
}

/**
 * 拉一次 M1 筛选元数据，并区分「还没回来」与「拿不到」。
 *
 * 分开是给页面用的：两种情况的下拉都走静态兜底，但失败时要挂一条提示，
 * 否则用户选不到某个来源时会以为是数据不存在。
 */
export function useMetaFiltersState(): PmMetaState {
  const [meta, setMeta] = useState<PmMetaFilters | null>(() => cached);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (cached) {
      setMeta(cached);
      return;
    }
    let alive = true;
    void loadMeta().then((data) => {
      if (!alive) return;
      if (data) setMeta(data);
      else setFailed(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  return { meta, failed };
}

/**
 * 只要候选值的简版：拿不到就是 null，调用方走静态兜底。
 * 需要区分失败态的页面用 `useMetaFiltersState`。
 */
export default function useMetaFilters(): PmMetaFilters | null {
  return useMetaFiltersState().meta;
}
