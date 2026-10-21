import apiClient from './request';

/**
 * 任务中心的六个接口（设计文档 §4.4，后端唯一开给前端的口子）：两个只读 + 四个写。
 *
 * 字段口径的权威定义在 `capabilities/task_center/dtos/console.py`，此处逐字对齐。
 * **没有删除 / 详情接口** —— 再加之前先回设计文档确认边界。
 */

/** 任务状态。与子任务状态**不是同一套**取值：成功在这里叫 completed。 */
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'stopped';

/**
 * 子任务状态。成功叫 success，且多一个 timeout。
 *
 * `stopped` 是**管理方置的终态**（2026-10-15）：用户停整条任务时判给还没开始的那些步骤，
 * 或停某一步时按 `downstream_on_stop` 连带判给下游 —— 运行方报不了它。
 */
export type SubtaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'timeout' | 'stopped';

export interface TaskConsoleItem {
  id: string;
  /** 发起时没给就为空；后端会退化成编排定义名。 */
  task_name: string | null;
  /**
   * 任务来源（后端 `TaskConsoleItem.task_source`）。取值 ∈ 协议层 `TASK_SOURCES`，
   * 如 `knowledge_base.file.manual_upload`。
   *
   * ⚠️ **存的是码不是文案**，中文名由页面按码映射（见 `TaskCenter/constant.ts` 的
   * `taskSourceLabel`）。**空 = 不是运行方提交的**（定时触发 / 人工立即触发），
   * 或这一列诞生之前的存量行 —— 不是漏记。
   */
  task_source?: string | null;
  status: TaskStatus;
  /** ISO 8601。 */
  started_at: string | null;
  finished_at: string | null;
  /** 耗时整秒数；还没跑完就是空。 */
  duration_seconds: number | null;
}

export interface TaskConsolePage {
  items: TaskConsoleItem[];
  page: number;
  page_size: number;
  total: number;
  /** 四个状态键恒在（没有的为 0）。只受「除 status 外」的筛选条件影响。 */
  status_counts: Record<string, number>;
}

/**
 * 一条「引用」：`kind` + `params`，**没有正文、没有地址**（协议层 2026-10-09 拍板）。
 *
 * `kind` 带运行方前缀（如 `knowledge_base.document`），前端按它决定「这是什么、
 * 去哪儿看」；`params` 是运行方自己接口要的字段，每个 kind 要哪些写死在协议层
 * `INPUT_REF_PARAMS` / `OUTPUT_REF_PARAMS`。
 */
export interface SubtaskRef {
  kind: string;
  params: Record<string, unknown>;
}

export interface SubtaskConsoleItem {
  id: string;
  /**
   * 这条子任务在编排里的节点名（如 `parse_1`）。
   *
   * ⚠️ **不是**同一行的 `id`：`stopSubtask` / `retrySubtask` 只用它寻址，`id` 在页面上
   * 只用于展示与 React key。这两个 id 长得像、含义完全不同，混用会打到别人身上。
   */
  node_id: string;
  /** 内置清单里的步骤名（文件解析 / 知识抽取 / 知识编译），查不到才退化成 step_code。 */
  subtask_name: string | null;
  status: SubtaskStatus;
  /**
   * 这一步自己的失败原因（判死 / 运行方回失败时写）。成功、或还在跑就是 `null`。
   *
   * ⚠️ 与任务列表那一行的 `error_message` **不是同一份**：那条是失败节点的这条拼出来的
   * （`<节点名>: <原因>`），条数多一条少一条看的是失败节点有几个。
   */
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  /**
   * 输入「**从哪儿来**」的引用（2026-10-16 加，与 `output_refs` 对称）。
   *
   * **只有终态上报那一刻才有值**：`running` 期间为空 —— 输入指向哪个页面只有运行方知道，
   * 只能等它上报。老运行方不上报时同样为空，页面按 `?? []` 处理，不把它读成「没有输入」。
   */
  input_refs?: SubtaskRef[] | null;
  /**
   * 产出「**去哪儿看**」的引用。**只给入口、不给内容** —— 正文与它的权限都留在运行方那侧，
   * 页面据 `kind` 查本地映射（见 `pages/TaskCenter/resultRefs.ts`）跳到对应子应用页面。
   *
   * 空（null / 空数组）= 没东西可看：老运行方不上报，或这一步还没终态。
   */
  output_refs?: SubtaskRef[] | null;
}

/**
 * 四个写接口的返回：**内部契约 `TaskItemResponse` 的故意子集**。
 *
 * ⚠️ 它**不是** `TaskConsoleItem` —— 那份是列表 DTO，多一个 `duration_seconds`。
 * 所以拿到它**不要把整个对象摊进页面的 `detail` 状态**（摊了 `duration_seconds` 就没了），
 * 页面只取 `status` 用来更新门禁。完整形状见 `jonex_core/task_protocol/models.py`。
 */
export interface TaskWriteResult {
  id: string;
  status: TaskStatus;
}

export interface TaskListParams {
  /** 模糊匹配任务名或任务 id。 */
  keyword?: string;
  /**
   * 按任务来源筛（∈ 协议层 `TASK_SOURCES`）。**单值精确匹配**，非法取值后端当场回 1001。
   *
   * ⚠️ 筛它永远筛不到平台侧自己触发的任务 —— 那些行这一列为空（后端口径：
   * 「有值才加条件」）。
   */
  task_source?: string;
  /**
   * 领域空间（取值是**主应用当前选中的空间**，不是页面上的下拉）。
   *
   * ⚠️ 后端 `page_tasks` 目前**不接收** —— 它只取 keyword / start_time / end_time /
   * statuses / page / page_size，多传的键被 dispatch 那层逐个取值时静默忽略。
   * 照发是既定决定：前端一次接好，后端补齐后自动生效。
   *
   * 注意任务**确实有空间归属**（`retry_subtask` 的 `space_id` 是必传的归属凭据，
   * 见 `_require_space_match`），所以这个参数不是凭空加的维度，只是列表口还没开。
   */
  space_id?: string;
  /** 按**建任务时间**筛（含两端）。 */
  start_time?: string;
  end_time?: string;
  /** 多选。页面当前是单选，传单元素数组。 */
  status?: TaskStatus[];
  page?: number;
  page_size?: number;
}

export async function listTasks(params: TaskListParams): Promise<TaskConsolePage> {
  // status 是数组：axios 默认序列化成 `status[]=a&status[]=b`，后端认的是可重复参数
  // （`?status=a&status=b`），所以必须显式指定 indexes: null。
  // 已实测：默认 `status%5B%5D=a`，加了这个选项才是 `status=a&status=b`。
  const query: Record<string, unknown> = { ...params };
  if (!params.status?.length) {
    delete query.status;
  }
  return apiClient.get<TaskConsolePage>('/task-center/tasks', {
    params: query,
    paramsSerializer: { indexes: null },
  });
}

/** 子任务列表。**不分页、回裸数组**（设计文档 §4.0 点名的「不设分页」接口）。 */
export async function listSubtasks(taskId: string): Promise<SubtaskConsoleItem[]> {
  return apiClient.get<SubtaskConsoleItem[]>(`/task-center/tasks/${taskId}/subtasks`);
}

/**
 * 停止任务。任务必须是 `pending` / `running`，且底下还要有没开始的子任务。
 *
 * 失败有两种拒绝情形（已终态 / running 但无 pending 子任务），都回同一个冲突码，
 * 调用方不需要自己区分 —— 直接展示后端返回的 `message` 即可。
 */
export async function stopTask(taskId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/stop`);
}

/**
 * 重跑整条任务（原地重来，不新建任务）。
 *
 * 前提是任务**已终态**（`completed` / `failed`），否则后端回 1003 —— 页面对应地用
 * `TASK_STATUS_*` 判终态来收敛按钮，不让用户点了才被拒。
 */
export async function retryTask(taskId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/retry`);
}

/**
 * 停止某一步。`nodeId` 传的是**节点名**（`SubtaskConsoleItem.node_id`，如 `parse_1`），
 * 不是子任务那一行的 `id` —— 后端按 node_id 寻址。
 *
 * 只有 `pending` 的那一步能停（`running` 的停不掉，运行方没有停止动作）。
 * ⚠️ 停完可能把**整条任务**收口成 `failed`，所以返回值里的 `status` 要消费掉。
 */
export async function stopSubtask(taskId: string, nodeId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/subtasks/${encodeURIComponent(nodeId)}/stop`);
}

/**
 * 重跑某一步。`nodeId` 同样是**节点名**，不是行 id。
 *
 * 前提三条：任务已终态、这一步也跑完了、它的上游全部成功 —— 后一条前端判不了
 * （控制台 DTO 不含 `depends_on`），交给后端拒并原样展示 `message`。
 * ⚠️ 重跑会把任务拉回 `running`，兄弟步骤的重试链接要立刻消失（靠调用方写后刷新）。
 */
export async function retrySubtask(taskId: string, nodeId: string): Promise<TaskWriteResult> {
  return apiClient.post<TaskWriteResult>(`/task-center/tasks/${taskId}/subtasks/${encodeURIComponent(nodeId)}/retry`);
}

/**
 * 任务中心的两个权限码（照 `api/tenants.ts` 的写法，常量与接口同处一个文件）。
 *
 * scope 是**租户级**：平台管理员与租户管理员持码，普通用户不持。
 * 它们不只是入口可见性 —— 能力侧 `capabilities/task_center/capability.py` 的
 * `_ACTION_PERMISSIONS` 真的按这两个码判权（read 管两个只读接口，write 管**四个**写
 * action：`task.stop` / `task.console_task_retry` / `task.console_subtask_stop` /
 * `task.console_subtask_retry`）。
 */
export const TASK_CENTER_READ_PERMISSION = 'task_center:read';
export const TASK_CENTER_WRITE_PERMISSION = 'task_center:write';
