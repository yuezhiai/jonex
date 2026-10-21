import { Button, Empty, Space, Spin, Tag, Timeline, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { TaskStatus } from '../../api/taskCenter';
import { SUBTASK_STATUS_COLORS, SUBTASK_STATUS_DOT_COLORS, SUBTASK_STATUS_LABEL_KEY, subtaskAction } from '../../constants/taskCenter';
import { fmtDateTime, fmtDuration } from './format';
import SubtaskResult from './SubtaskResult';
import useSubtasks from './useSubtasks';

/**
 * 子任务动作的回调。带任务 id 是因为同一套回调同时服务展开行与抽屉（两个不同的任务）；
 * 带名字是为了弹窗标题能指名道姓（`node_id` 用户看不懂）。
 */
export type SubtaskActionHandler = (taskId: string, nodeId: string, name: string) => void;

interface Props {
  taskId: string;
  /** 任务当前状态：子任务能给什么动作由它 + 子任务自身状态共同决定。 */
  taskStatus: TaskStatus;
  /** 有没有 `task_center:write`。没有就连链接都不渲染。 */
  canWrite: boolean;
  /**
   * 父组件在每次写操作后 +1，用来触发这里重取 —— `CommonTable.refresh()` 只刷表格行，
   * 不会重新挂载展开行的子节点（`rowKey` 稳定），所以刷新得靠这个信号显式传下来。
   */
  reloadToken: number;
  onStopSubtask: SubtaskActionHandler;
  onRetrySubtask: SubtaskActionHandler;
  indent?: number;
}

/**
 * 子任务列表（展开行用）。
 *
 * 取数与自动轮询走 `useSubtasks`（与抽屉共用）；这里只管 Timeline 怎么画。
 */
export default function TaskSubtasks({
  taskId,
  taskStatus,
  canWrite,
  reloadToken,
  onStopSubtask,
  onRetrySubtask,
  indent = 48,
}: Props) {
  const { t } = useTranslation();
  const { items, loading, error, reload } = useSubtasks(taskId, reloadToken);

  // 只在「还没有数据」时整块换 Spin。轮询会持续重取，若 loading 就把列表卸载，
  // 用户正看着的时间线会每几秒闪一次
  if (loading && !items.length) {
    return (
      <div style={{ padding: 16, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  if (error) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.loadFailed')}>
        <Button onClick={reload}>{t('common.retry')}</Button>
      </Empty>
    );
  }

  if (!items.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />;
  }

  return (
    <div style={{ marginLeft: Math.max(0, indent - 18), paddingLeft: 16 }}>
      <Timeline
        items={items.map((item) => {
          const action = canWrite ? subtaskAction(taskStatus, item.status) : null;
          const name = item.subtask_name ?? item.node_id;
          return {
            // status 传不进来（useItems 按 loading 覆写它），所以「处理中」走 loading 这条语义通道，
            // 其余状态只能靠点色区分
            loading: item.status === 'running',
            color: SUBTASK_STATUS_DOT_COLORS[item.status],
            content: (
              // 竖排两层：上面一行是本来的「名称 / 状态 / 时刻 / 耗时 / 动作」，
              // 失败原因另起一行（它有长有短，挤进那一行会把时间线撑烂）
              <Space direction="vertical" size={4}>
                <Space size={12}>
                  <span>{item.subtask_name ?? '-'}</span>
                  <Tag color={SUBTASK_STATUS_COLORS[item.status]}>{t(SUBTASK_STATUS_LABEL_KEY[item.status])}</Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {fmtDateTime(item.started_at)}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {fmtDuration(item.duration_seconds)}
                  </Typography.Text>
                  {/* 动作回调带的是 node_id（写接口的寻址键），不是 item.id */}
                  {action === 'stop' && (
                    <Typography.Link onClick={() => onStopSubtask(taskId, item.node_id, name)}>
                      {t('taskCenter.action.stop')}
                    </Typography.Link>
                  )}
                  {action === 'retry' && (
                    <Typography.Link onClick={() => onRetrySubtask(taskId, item.node_id, name)}>
                      {t('taskCenter.action.retry')}
                    </Typography.Link>
                  )}
                  {/* 执行结果跟着引用走（列表已带上 input_refs / output_refs），点开不再发请求；
                      组件内部只在 success 时渲染入口，非成功状态这一项自己消失 */}
                  <SubtaskResult item={item} />
                </Space>
                {/* 原因用红字、不套 Alert：时间线里每条都顶一个卡片框会糊成一片，
                    这里只需要「这一条为什么失败」在视觉上跳出来 */}
                {item.error_message && <Typography.Text type="danger">{item.error_message}</Typography.Text>}
              </Space>
            ),
          };
        })}
      />
    </div>
  );
}
