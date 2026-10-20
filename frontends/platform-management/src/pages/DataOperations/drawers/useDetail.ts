import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getErrorMessage, type ApiError } from '@jonex/shared-lib';

/**
 * 抽屉的失败态。
 *
 * `notFound` 单列出来，是因为四个接口的 404 有特殊语义：A/B/C 把「不存在 / 已删除 /
 * 无权限 / 不在本空间」并成同一个 404（防探测），前端**不能**把它提示成「无权限」
 * —— 那等于替后端确认了这个 id 存在。所以 404 不展示服务端文案，改用固定说法。
 */
export interface DetailError {
  notFound: boolean;
  message: string;
}

/**
 * 抽屉取数：`target` 非空即请求，为空（关闭）时不发请求也不清数据
 * —— 关闭动画期间清空会让抽屉内容先闪成空白。
 *
 * `load` 必须是 useCallback 包过的稳定引用（同 useAsyncData 的约定），且其依赖里
 * **要带上页面窗口**：窗口一变，抽屉里的「引用/命中/检索次数」就与列表那一行不再是
 * 同一个口径，必须跟着重取。
 */
export default function useDetail<T, K>(target: K | null, load: (target: K) => Promise<T>) {
  const { t } = useTranslation();
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<DetailError | null>(null);
  // 重试：改这个数让 effect 重跑，不必把 load 的依赖搅进状态
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (target == null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    load(target)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        const apiError = e as ApiError;
        setError({
          notFound: apiError?.status === 404,
          message: getErrorMessage(e, t('dataOperations.error.loadFailed')),
        });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target, load, attempt, t]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return { data, loading, error, retry };
}
