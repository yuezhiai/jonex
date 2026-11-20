/**
 * 任务中心的两级路由。
 *
 * 一级 = 任务列表（任务中心的落地页，侧栏与顶栏入口都指向它）；
 * 二级 = 某个任务定义下的执行记录（原有任务中心页面整体下移到这里，接口零改动）。
 *
 * 二级用两个形态：
 * - `/task-center/executions`：不绑定具体任务的执行记录全量列表。平台监控的
 *   「前往任务中心」深链落在这里 —— 它是「一批失败任务」的视角，不是某一个任务；
 * - `/task-center/:definitionId`：从一级列表某一行进来，带该任务的名称做标题。
 *
 * 编辑任务是二级页的下一层：`/task-center/:definitionId/edit`，同样走静态段优先，
 * 不会与执行记录路由互相吞掉。
 *
 * `executions` 是静态段，react-router 的匹配优先级高于 `:definitionId`，
 * 两者不会互相吞掉（只有 id 恰好叫 `executions` 时才撞车，假数据里不存在）。
 */
export const TASK_CENTER_LIST_PATH = '/task-center';

/** 二级页的「全部执行记录」路径。带上 query（status / from / to）即平台监控的深链形态。 */
export const TASK_EXECUTIONS_ALL_PATH = '/task-center/executions';

/** 某个任务定义的执行记录路径。id 进 URL 要编码，避免 id 里出现 `/` 时把路由拆开。 */
export const taskExecutionsPath = (definitionId: string): string => `/task-center/${encodeURIComponent(definitionId)}`;

/**
 * 任务编辑（编排）路径。
 *
 * `edit` 与上面的 `executions` 一样是静态段，react-router 的静态优先规则保证
 * `/task-center/def_x/edit` 落到编辑页而不是 `:definitionId` 的执行记录页。
 */
export const taskEditorPath = (definitionId: string): string =>
  `/task-center/${encodeURIComponent(definitionId)}/edit`;
