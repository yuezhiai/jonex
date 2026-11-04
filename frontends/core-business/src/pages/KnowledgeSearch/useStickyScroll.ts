/**
 * [jonex] 结果区「流式跟随」滚动。
 *
 * 需求（用户原话）：流式时要自动滑到最新；一旦人自己动过滚动，就停在他要看的
 * 位置，不要抢。所以这里维护的是一个**吸附（pin）状态**，而不是无条件的
 * `scrollToBottom`：
 *
 *   - 吸附中：内容变高就跟到底（直接落位，不做 smooth 动画 —— 流式正文每帧都在长，
 *     平滑动画会互相打断，反而比直接落位更抖）；
 *   - 用户滚动（wheel / touch / 键盘）且离开底部超过阈值 → 解除吸附，停在原地；
 *     期间新内容只点亮「有新内容」，不再挪动视口；
 *   - 用户自己滚回底部（≤ 8px）→ 重新吸附；点「回到最新」也重新吸附。
 *
 * 判定用户意图用的是**事件来源 + 时间窗**：只有 wheel/touch/key 之后的一小段时间内
 * 的 scroll 才算「人动的」，其余 scroll 是程序自己滚的，不能据此解除吸附。
 * 阈值（24px）是为了容差：触控板惯性、像素取整都会让 scrollTop 抖动几个像素。
 */
import { useCallback, useEffect, useRef, useState } from 'react';

/** 离开底部超过这个距离才认为「用户想看上面」 */
export const STICK_THRESHOLD = 24;
/** 距离底部小于这个值就算「在底部」 */
export const AT_BOTTOM_SLACK = 8;
/** wheel/touch/key 之后多久内的 scroll 算作用户意图 */
const INTENT_WINDOW_MS = 600;

/** 纯判定：当前是否应该解除吸附（导出便于单测） */
export function shouldUnpin(distance: number, userInitiated: boolean, threshold = STICK_THRESHOLD): boolean {
  return userInitiated && distance > threshold;
}

/** 纯判定：当前是否算「已经回到底部」 */
export function isAtBottom(distance: number, slack = AT_BOTTOM_SLACK): boolean {
  return distance <= slack;
}

export interface StickyScrollOptions {
  intentWindowMs?: number;
}

export interface StickyScroll {
  /** 是否吸附在底部 */
  pinned: boolean;
  /** 解除吸附期间又来了新内容 */
  hasNew: boolean;
  /** 滚回最新（点提示条时用，走 smooth） */
  scrollToLatest: () => void;
  /** 切换会话时重置：pin=true 表示继续跟随（进行中的检索），false 表示停在顶部（历史回放） */
  reset: (pin: boolean) => void;
}

export function useStickyScroll(
  containerRef: React.RefObject<HTMLElement | null>,
  contentRef: React.RefObject<HTMLElement | null>,
  options: StickyScrollOptions = {},
): StickyScroll {
  const { intentWindowMs = INTENT_WINDOW_MS } = options;
  const [pinned, setPinned] = useState(true);
  const [hasNew, setHasNew] = useState(false);

  // 用 ref 镜像：ResizeObserver / scroll 回调里要读到最新值，不能依赖闭包里的 state
  const pinnedRef = useRef(true);
  const intentAtRef = useRef(0);
  const frameRef = useRef<number | null>(null);
  /**
   * 切会话后的静默窗口：历史回放是一次性把内容换掉，高度变化并不代表「有新进展」。
   * 只压制提示，不压制吸附——运行中的检索换会话后仍要立刻跟随。
   */
  const quietUntilRef = useRef(0);

  const applyPinned = useCallback((value: boolean) => {
    pinnedRef.current = value;
    setPinned((prev) => (prev === value ? prev : value));
    if (value) setHasNew(false);
  }, []);

  const scrollToLatest = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (frameRef.current != null) cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    });
    applyPinned(true);
  }, [applyPinned, containerRef]);

  const reset = useCallback((pin: boolean) => {
    const el = containerRef.current;
    applyPinned(pin);
    setHasNew(false);
    if (!pin) quietUntilRef.current = Date.now() + 600;
    if (!el) return;
    if (frameRef.current != null) cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      el.scrollTop = 0;
    });
  }, [applyPinned, containerRef]);

  // ① 用户意图 + 回到底部判定
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;

    const markIntent = () => {
      intentAtRef.current = Date.now();
    };
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (isAtBottom(distance)) {
        if (!pinnedRef.current) applyPinned(true);
        return;
      }
      const userInitiated = Date.now() - intentAtRef.current < intentWindowMs;
      if (pinnedRef.current && shouldUnpin(distance, userInitiated)) applyPinned(false);
    };

    el.addEventListener('wheel', markIntent, { passive: true });
    el.addEventListener('touchmove', markIntent, { passive: true });
    el.addEventListener('keydown', markIntent);
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      el.removeEventListener('wheel', markIntent);
      el.removeEventListener('touchmove', markIntent);
      el.removeEventListener('keydown', markIntent);
      el.removeEventListener('scroll', onScroll);
    };
  }, [applyPinned, containerRef, intentWindowMs]);

  // ② 内容变高：吸附时跟到底，否则只标记「有新内容」
  useEffect(() => {
    const content = contentRef.current;
    const el = containerRef.current;
    if (!content || !el || typeof ResizeObserver === 'undefined') return undefined;

    const observer = new ResizeObserver(() => {
      // 切换会话瞬间的高度变化是「内容被替换」，不是新进展：不提示
      if (Date.now() < quietUntilRef.current) return;
      if (!pinnedRef.current) {
        setHasNew(true);
        return;
      }
      // 放进下一帧：避免在 ResizeObserver 回调里同步改 scrollTop 触发
      // “ResizeObserver loop completed with undelivered notifications”
      if (frameRef.current != null) return;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        el.scrollTop = el.scrollHeight;
      });
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
      if (frameRef.current != null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [containerRef, contentRef]);

  return { pinned, hasNew, scrollToLatest, reset };
}
