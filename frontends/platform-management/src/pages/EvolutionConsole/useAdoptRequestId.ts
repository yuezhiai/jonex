import { useRef } from 'react';

/** 采纳幂等键：每次「确认采纳」首击生成一次，失败/超时重试复用，成功清空。 */
export const newRequestId = (): string => {
  const uuid = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `adopt_${uuid}`;
};

/**
 * 幂等键的作用域（C4-2）：不同勾选 / override 理由 / ad-hoc truth 集合
 * 是**不同的采纳意图**，不能复用同一个 request_id；同一意图的重试才复用。
 */
export const adoptRequestKey = (
  picks: string[],
  overrideReason: string,
  truthIds: string[],
): string => JSON.stringify([
  [...picks].sort(),
  overrideReason.trim(),
  [...truthIds].sort(),
]);

export interface AdoptRequestIdHolder {
  /** 取得当前 key 的幂等键；key 变化 → 丢弃旧 id 并重新生成。 */
  ensure(key: string): string;
  /** 成功后清空，下一次确认重新生成。 */
  reset(): void;
  /** 读取当前 key 的幂等键（没有则 null）；不生成。 */
  peek(key: string): string | null;
}

/** 纯状态容器（可单测）：把「key → request_id」的作废规则从 React 中剥离。 */
export function createAdoptRequestIdHolder(): AdoptRequestIdHolder {
  let currentKey: string | null = null;
  let currentId: string | null = null;
  return {
    ensure(key: string): string {
      if (currentKey !== key) {
        currentKey = key;
        currentId = null;
      }
      if (!currentId) currentId = newRequestId();
      return currentId;
    },
    reset() {
      currentId = null;
    },
    peek(key: string): string | null {
      return currentKey === key ? currentId : null;
    },
  };
}

/** RunDetail 用的 hook：holder 跨渲染保持，key 变化时自动作废旧 id。 */
export function useAdoptRequestId(): AdoptRequestIdHolder {
  const holder = useRef<AdoptRequestIdHolder | null>(null);
  if (!holder.current) holder.current = createAdoptRequestIdHolder();
  return holder.current;
}
