import { useCallback, useEffect, useRef, useState } from 'react';
import { getWikiCompilationState, setWikiCompilationPaused } from '@/api/domainKnowledge';
import type { WikiCompilationState } from '@/api/domainKnowledge';
import { wikiCompilationStatus } from '@/utils/wikiCompilationState';

export function useWikiCompilation(kbId: string, enabled: boolean) {
  const [state, setState] = useState<WikiCompilationState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const sequence = useRef(0);
  const mutating = useRef(false);
  const currentKb = useRef(kbId);
  currentKb.current = kbId;
  // 上一份状态的签名：轮询每 5 秒都会拿到一个新对象，但状态没变时不该 setState——
  // 下游（DocumentLibrary）依赖 state 引用变化来重取文档列表，无脑换对象会让
  // 列表每 5 秒跟着刷一次（loading / 请求噪音，见 2026-11-12 真机反馈）。
  const lastSignature = useRef('');
  const refresh = useCallback(async () => {
    if (mutating.current) return;
    const request = ++sequence.current;
    try {
      const next = await getWikiCompilationState(kbId);
      if (currentKb.current === kbId && request === sequence.current) {
        const signature = JSON.stringify(next);
        if (signature !== lastSignature.current) {
          lastSignature.current = signature;
          setState(next);
        }
        setError(false);
      }
    } catch {
      if (currentKb.current === kbId && request === sequence.current) setError(true);
    }
  }, [kbId]);
  useEffect(() => {
    lastSignature.current = '';
    setState(null); setError(false); setLoading(false);
    if (!enabled || !kbId) return;
    let alive = true;
    let timer: number;
    const poll = async () => {
      await refresh();
      if (alive) timer = window.setTimeout(poll, 5000);
    };
    void poll();
    return () => { alive = false; ++sequence.current; window.clearTimeout(timer); };
  }, [kbId, enabled, refresh]);
  const status = wikiCompilationStatus(state, error);
  const canToggle = !!state && !error && !loading && (state.paused || ['active', 'queued', 'waiting', 'parsing'].includes(status));
  const toggle = useCallback(async () => {
    if (!state || !canToggle || mutating.current) return;
    mutating.current = true;
    const request = ++sequence.current;
    setLoading(true);
    try {
      const next = await setWikiCompilationPaused(kbId, !state.paused);
      if (currentKb.current === kbId && request === sequence.current) {
        setState(next); setError(false);
      }
    } catch {
      if (currentKb.current === kbId && request === sequence.current) setError(true);
    } finally {
      mutating.current = false;
      if (currentKb.current === kbId) setLoading(false);
    }
  }, [kbId, state, canToggle]);
  return { state, loading, error, status, canToggle, refresh, toggle };
}
