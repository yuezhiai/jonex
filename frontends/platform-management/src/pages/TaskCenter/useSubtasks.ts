import { useCallback, useEffect, useRef, useState } from 'react';
import { listSubtasks } from '../../api/taskCenter';
import type { SubtaskConsoleItem } from '../../api/taskCenter';
import { TASK_POLL_INTERVAL_MS, backoffPollInterval, isSubtaskTerminal } from '../../constants/taskCenter';
import useSilentPolling from './useSilentPolling';

interface UseSubtasksResult {
  items: SubtaskConsoleItem[];
  loading: boolean;
  /** 非静默取数的错误。静默（轮询）失败不会走到这里，见下。 */
  error: unknown;
  /** 手动重取，给错误兜底页的「重试」。走非静默：用户点了就该看到加载反馈。 */
  reload: () => void;
}

/**
 * 一个任务的子任务列表：取数 + 自动轮询。展开行与抽屉两处共用。
 *
 * 抽出来是因为两处**必须逐字同步**的东西变多了 —— 序号保护、静默重取、失败退避，
 * 各写一份迟早只改一处。展示形态仍是各自的（Timeline / Collapse），那部分不在这里。
 *
 * 调用的两处都只在「展开着 / 抽屉开着」时挂载，所以这里天然是懒加载：折叠即卸载，
 * 定时器跟着消失，不需要额外看管可见性。
 *
 * 不传 `attempt`：接口支持 `?attempt=N`，但列表接口返回的任务对象里没有任何 attempt
 * 信息，页面无从知道该传第几次，走「最新一次运行」的默认。
 */
export default function useSubtasks(taskId: string, reloadToken: number): UseSubtasksResult {
  const [items, setItems] = useState<SubtaskConsoleItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [pollInterval, setPollInterval] = useState(TASK_POLL_INTERVAL_MS);
  /**
   * 序号管两件事：并发请求的乱序（轮询与写操作后的重取会撞上，慢的旧请求可能后返回）、
   * 以及卸载后的迟到响应不再写 state。
   */
  const seqRef = useRef(0);

  const load = useCallback(
    (silent: boolean) => {
      const seq = ++seqRef.current;
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      return listSubtasks(taskId)
        .then((rows) => {
          if (seq !== seqRef.current) return;
          setItems(rows ?? []);
          // 通一次就回到基准节奏
          setPollInterval(TASK_POLL_INTERVAL_MS);
        })
        .catch((e: unknown) => {
          if (seq !== seqRef.current) return;
          if (silent) {
            // 轮询赶上的失败：留着手上这份数据，只把下一拍推远。清空列表、切错误态、
            // 弹提示都会让「用户没要求的那次刷新」反过来打断用户
            setPollInterval(backoffPollInterval);
            return;
          }
          setItems([]);
          setError(e);
        })
        .finally(() => {
          if (seq === seqRef.current && !silent) setLoading(false);
        });
    },
    [taskId],
  );

  // 首次挂载，以及每次写操作后（父组件把 reloadToken +1）都走非静默：这些是用户主动触发的
  useEffect(() => {
    void load(false);
  }, [load, reloadToken]);

  // 卸载即作废在途请求：序号一并前推，迟到的响应落不到已卸载的组件上
  useEffect(
    () => () => {
      seqRef.current += 1;
    },
    [],
  );

  const silentReload = useCallback(() => {
    void load(true);
  }, [load]);

  // 还有没跑完的就继续刷。写在取数之后 —— 这个判据是从 items 算出来的
  const hasRunning = items.some((item) => !isSubtaskTerminal(item.status));
  useSilentPolling(hasRunning, silentReload, pollInterval);

  const reload = useCallback(() => {
    void load(false);
  }, [load]);

  return { items, loading, error, reload };
}
