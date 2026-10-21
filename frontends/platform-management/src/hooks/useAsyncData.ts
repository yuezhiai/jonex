import { useCallback, useEffect, useState } from 'react';

/** 把 catch 到的东西收敛成 Error，页面拿到的错误形状只有一种 */
function toError(value: unknown): Error {
  if (value instanceof Error) return value;
  return new Error(typeof value === 'string' ? value : String(value));
}

/**
 * 按 load 的引用变化重新取数，并在取数期间给出 loading。
 *
 * load 必须是用 useCallback 包过的稳定引用，否则每次渲染都会重新取一遍。
 * 用 cancelled 丢弃过期响应：快速连点切换时，旧请求的结果可能晚于新请求到达。
 *
 * **失败要能被看见**：早期实现把异常吞掉、只把 data 置空，于是「接口挂了」在页面上
 * 长得跟「确实没有数据」一模一样。现在错误原样回给调用方，由它决定挂提示还是退空态。
 * `data` 在失败时**保留上一份**：重取失败不等于上一份数据作废，清空只会让图表/表格
 * 从有内容闪成空态。
 */
export default function useAsyncData<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // 重试计数器：load 的引用没变（如只是「再试一次」），只能另加一个触发源
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    load()
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(toError(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, loading, error, reload };
}
