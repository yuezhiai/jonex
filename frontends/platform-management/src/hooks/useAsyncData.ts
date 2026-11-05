import { useCallback, useEffect, useRef, useState } from 'react';

/** 把 catch 到的东西收敛成 Error，页面拿到的错误形状只有一种 */
function toError(value: unknown): Error {
  if (value instanceof Error) return value;
  return new Error(typeof value === 'string' ? value : String(value));
}

export interface UseAsyncDataOptions {
  /**
   * 变化时**静默重取**：不动 loading、不清空已有错误，失败也保留上一份数据。
   *
   * 给定时刷新用 —— 每 60 秒闪一次骨架屏 / 表格遮罩不算「刷新」，只会让人以为页面在重载。
   * 与 `load` 引用同时变化时按非静默处理：那是用户改了查询条件，需要转场反馈。
   */
  silentKey?: unknown;
}

/**
 * 按 load 的引用变化重新取数，并在取数期间给出 loading。
 *
 * load 必须是用 useCallback 包过的稳定引用，否则每次渲染都会重新取一遍。
 * 用请求序号 + cancelled 丢弃过期响应：快速连点切换时，旧请求的结果可能晚于新请求到达。
 *
 * **失败要能被看见**：早期实现把异常吞掉、只把 data 置空，于是「接口挂了」在页面上
 * 长得跟「确实没有数据」一模一样。现在错误原样回给调用方，由它决定挂提示还是退空态。
 * `data` 在失败时**保留上一份**：重取失败不等于上一份数据作废，清空只会让图表/表格
 * 从有内容闪成空态。
 */
export default function useAsyncData<T>(load: () => Promise<T>, options: UseAsyncDataOptions = {}) {
  const { silentKey } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // 重试计数器：load 的引用没变（如只是「再试一次」），只能另加一个触发源
  const [nonce, setNonce] = useState(0);
  // 请求序号：两次 effect 各自的响应都可能晚到，只接受最后一次发出的结果
  const seqRef = useRef(0);
  // 上一次触发的来源：只有 load / nonce 都没变、单凭 silentKey 触发才算静默重取
  const prevTriggerRef = useRef<{ load: () => Promise<T>; nonce: number; silentKey: unknown } | null>(null);

  useEffect(() => {
    const prev = prevTriggerRef.current;
    // 只比「上次的来源」不够：StrictMode 会把挂载期的 effect 跑两遍（cleanup 后再跑），
    // load / nonce 都没变，于是首屏那次取数会被误判成静默、骨架屏一闪就没。
    // 再要求 silentKey 本身变过，真静默才成立。
    const silent = prev !== null && prev.load === load && prev.nonce === nonce && !Object.is(prev.silentKey, silentKey);
    prevTriggerRef.current = { load, nonce, silentKey };

    let cancelled = false;
    const seq = ++seqRef.current;
    // 静默重取不动 loading：页面正看着的这份数据要原地换掉，不该整块罩上骨架屏/遮罩
    if (!silent) {
      setLoading(true);
      setError(null);
    } else {
      // 静默请求可能顶掉一个还在飞的显式请求（它已因序号失配被丢弃，收不到自己的收尾）：
      // 不清掉 loading 就会一直罩着骨架屏
      setLoading(false);
    }
    load()
      .then((result) => {
        if (cancelled || seq !== seqRef.current) return;
        setData(result);
        // 静默取数成功也算恢复：上一轮的失败提示该撤掉，否则页面停在错误态直到手动刷新
        if (silent) setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled || seq !== seqRef.current) return;
        // 静默失败保留上一份数据与现有错误态：定时刷新失败不该把正在看的页面打成错误态
        if (!silent) setError(toError(reason));
      })
      .finally(() => {
        if (cancelled || seq !== seqRef.current) return;
        if (!silent) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, nonce, silentKey]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, loading, error, reload };
}
