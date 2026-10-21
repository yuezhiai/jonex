import type { SubtaskStatus, TaskStatus } from '../api/taskCenter';

/**
 * 状态映射表。任务与子任务是**两套独立取值**，不能互相替代：
 * 成功在任务上是 completed、在子任务上是 success，子任务还多一个 timeout。
 */

export const TASK_STATUS_LABEL_KEY: Record<TaskStatus, string> = {
  pending: 'taskCenter.status.pending',
  running: 'taskCenter.status.running',
  completed: 'taskCenter.status.completed',
  failed: 'taskCenter.status.failed',
  stopped: 'taskCenter.status.stopped',
};

/** 状态标签栏的顺序（不含「全部」——它由页面补在最前）。 */
export const TASK_STATUS_ORDER: readonly TaskStatus[] = [
  'pending',
  'running',
  'completed',
  'failed',
  'stopped',
];

/**
 * 状态 Tag 的颜色，按 antd 状态色语义取值：进行中用 processing（蓝）而不是 success（绿），
 * 绿色读作「已结束且成功」。
 *
 * `stopped` 取 warning 而不是 error：它不是「跑挂了」，是「被人停的」—— 用红色与 `failed`
 * 撞色的话，用户点完停止看到的还是「失败」，这一档独立出来就白做了（设计文档 §6.7①）。
 */
export const TASK_STATUS_COLORS: Record<TaskStatus, string> = {
  pending: 'default',
  running: 'processing',
  completed: 'success',
  failed: 'error',
  stopped: 'warning',
};

export const SUBTASK_STATUS_LABEL_KEY: Record<SubtaskStatus, string> = {
  pending: 'taskCenter.subtaskStatus.pending',
  running: 'taskCenter.subtaskStatus.running',
  success: 'taskCenter.subtaskStatus.success',
  failed: 'taskCenter.subtaskStatus.failed',
  timeout: 'taskCenter.subtaskStatus.timeout',
  stopped: 'taskCenter.subtaskStatus.stopped',
};

export const SUBTASK_STATUS_COLORS: Record<SubtaskStatus, string> = {
  pending: 'default',
  running: 'processing',
  success: 'success',
  failed: 'error',
  timeout: 'warning',
  stopped: 'warning',
};

/**
 * 「执行结果」入口是否出现：只有成功的步骤才有产物可看 —— 失败 / 超时 / 被停的没有，
 * 跑着的还没回报引用。放在这里而不是组件文件里，是为了让调用方判断留白时也走同一条规则。
 */
export function hasExecutionResult(item: { status: SubtaskStatus }): boolean {
  return item.status === 'success';
}

/**
 * 子任务流水线（Timeline）节点的颜色。与上面那张**不是**同一套取值，不能互相替代：
 * Timeline 只认 blue/red/green/gray 四个预设字面量，其余值会被当成 CSS 颜色塞进变量而静默失效。
 */
export const SUBTASK_STATUS_DOT_COLORS: Record<SubtaskStatus, string> = {
  pending: 'gray',
  running: 'blue',
  success: 'green',
  failed: 'red',
  // Timeline 没有「超时」这个色档，归到红：它与失败同样是需要人看的结局
  timeout: 'red',
  // 「已停止」用中性灰：它既不是成功也不是失败，红蓝两档都读错了意思
  stopped: 'gray',
};

/**
 * ── 写操作的门禁（与后端前置逐字对齐）──
 *
 * 放在这里而不是页面里：子任务的两个动作有**两处渲染点**（展开行的 Timeline、抽屉的
 * Collapse），判定散开迟早会各判一套。
 */

/**
 * 任务已终态。后端 `_require_terminal` 认的就是这三个，重跑的前置。
 *
 * ⚠️ **`stopped` 必须在这里**：停掉的任务后端是允许原地重跑的，前端不认它就会把「重试」
 * 按钮藏掉 —— 用户停错了想再跑一次，界面上没有出路（设计文档 §6.7⑤）。
 */
export function isTaskTerminal(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'stopped';
}

/**
 * 某一步现在能用哪个动作，`null` = 什么都不给。
 *
 * - 任务**没跑完**：只有还没开始的（`pending`）能停 —— `running` 的停不掉（运行方没有
 *   停止动作，后端拒 1003）。
 * - 任务**已终态**：跑完了的（`success` / `failed` / `timeout` / `stopped`）能重跑 ——
 *   没跑完的自然无从谈起。
 *
 * ⚠️ 「上游是否全 `success`」**判不了**：控制台 DTO 不含 `depends_on`，这一档交给后端
 * 回 1001、前端原样展示它的 `message`（后端已按 locale 译好）。
 */
export function subtaskAction(taskStatus: TaskStatus, subtaskStatus: SubtaskStatus): 'stop' | 'retry' | null {
  if (!isTaskTerminal(taskStatus)) {
    return subtaskStatus === 'pending' ? 'stop' : null;
  }
  return subtaskStatus === 'success' ||
    subtaskStatus === 'failed' ||
    subtaskStatus === 'timeout' ||
    subtaskStatus === 'stopped'
    ? 'retry'
    : null;
}

/**
 * ── 自动轮询 ──
 *
 * 只刷「还在动」的东西：终态到了就不会自己再变（任务只能由人重跑，子任务只能由人重发），
 * 定时器跟着停。这条判据比「任务是否终态」更细一层 —— 任务收口后已有的 `running` 子任务
 * 还在等上报或超时，那时任务行不动、子任务行还得刷，所以两处各判各的。
 */
export function isSubtaskTerminal(status: SubtaskStatus): boolean {
  return status === 'success' || status === 'failed' || status === 'timeout' || status === 'stopped';
}

/**
 * 轮询间隔。依据是后端后台循环每轮 3 秒（`TASK_CENTER_SCHEDULER_INTERVAL`）：
 * 比它更密不会更早看到变化，只是白问。
 */
export const TASK_POLL_INTERVAL_MS = 5000;

/** 退避上限。后端持续不可用时停在 1 分钟一次，别拖成更稀的探活。 */
export const TASK_POLL_MAX_INTERVAL_MS = 60000;

/** 失败一次就翻倍，封顶。成功会走回 `TASK_POLL_INTERVAL_MS`，由调用方负责。 */
export function backoffPollInterval(current: number): number {
  return Math.min(current * 2, TASK_POLL_MAX_INTERVAL_MS);
}

/**
 * ── 任务来源 ──
 *
 * 码的形状是 `{运行方 code}.{对象}.{通道}`，权威定义在后端协议层
 * `jonex_core/task_protocol/models.py` 的 `TASK_SOURCES`。
 */

/**
 * 列表筛选与详情共用的四个码，顺序与后端一致。
 *
 * 只列这四个：筛选是**单值精确匹配**，多给一个后端不认的选项只会筛出空结果。
 * 后端加了新运行方时，这里和下面那张表一起补。
 */
export const TASK_SOURCE_VALUES: readonly string[] = [
  'knowledge_base.file.manual_upload',
  'knowledge_base.file.api_pull',
  'knowledge_base.file.api_push',
  'knowledge_base.file.storage_upload',
];

/**
 * 来源码 → i18n key。
 *
 * 复用数据源接入页那套 `dataSource.*Access` 而不是另起一组：同一批码、同一个概念
 * （知识库数据源是怎么进来的），两处措辞各写各的迟早对不上。
 *
 * ⚠️ 表里存的是 **key 不是译文**，取值一律过下面的 `taskSourceLabel()`。
 */
export const TASK_SOURCE_LABEL_KEY: Record<string, string> = {
  'knowledge_base.file.manual_upload': 'dataSource.fileUploadAccess',
  'knowledge_base.file.api_pull': 'dataSource.apiAccess',
  'knowledge_base.file.api_push': 'dataSource.apiPushAccess',
  'knowledge_base.file.storage_upload': 'dataSource.storageAccess',
};

/**
 * 来源码 → 展示文案。三种情形各有各的必要：
 *
 * - **已知码**取文案；
 * - **未知码原样回** —— 后端加了新运行方而前端还没跟上时，界面显示那个码本身，
 *   既不空白，也不会漏出 `dataSource.xxx` 这种原始 key；
 * - **空值 = 平台自建** —— 定时触发 / 人工立即触发那批不是运行方送来的任务，
 *   这一列本来就空（含义见 `Task.task_source` 的注释），不是漏记。
 */
export function taskSourceLabel(t: (key: string) => string, code: string | null): string {
  if (!code) return t('taskCenter.source.platform');
  return t(TASK_SOURCE_LABEL_KEY[code] ?? code);
}
