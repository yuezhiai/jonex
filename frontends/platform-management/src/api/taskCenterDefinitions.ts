import apiClient from './request';
import type { TaskStatus } from './taskCenter';

/**
 * 任务定义（一级列表 / 编辑）的接口层。
 *
 * 字段口径的权威定义在 `capabilities/task_center/dtos/definition.py` 与
 * `dtos/catalog.py`；`api_gateway/routes/task_center.py` 的 `def.*` 一组逐字转发。
 * 编排（`FlowConfig`）是后端 `task_definition_versions.config` 的 JSON 形状，
 * 前端只认 2026-11-18 起的三段式 `start / steps / end`（旧结构由后端读时升级）。
 */

/** 定义状态。与 `task_definitions.status` 逐字一致。 */
export type DefinitionStatus = 'draft' | 'published' | 'disabled';

/** 定义归属：系统级（平台预置、只读）/ 空间级（用户可写）。 */
export type DefinitionScope = 'system' | 'space';

/** 编排保存的目标状态：存草稿 / 存并发布。 */
export type OrchestrationSaveStatus = 'draft' | 'published';

/**
 * 字段定义（`FieldSpec`）。`type` 用后端的小写字面量：
 * `string / number / boolean / file / object / array`。
 *
 * `abstract=true` 表示「只表达形状、不展开子字段」，前端显示为 Object / Array<Object>。
 */
export interface FieldSpec {
  type: string;
  required?: boolean;
  default?: unknown;
  enum?: unknown[];
  description?: string;
  abstract?: boolean;
  properties?: Record<string, FieldSpec>;
  items?: FieldSpec;
}

/** 编排里的一个节点。**连线与引用一律用 `node_id`**（`step_code` 只用来查内置步骤）。 */
export interface FlowStep {
  node_id: string;
  step_code: string;
  depends_on?: string[];
  next_steps?: string[];
  sort?: number;
  timeout_seconds: number;
  max_retries?: number;
  on_retry_exhausted?: 'terminate' | 'skip';
  bind_inputs?: Record<string, unknown>;
  downstream_on_retry?: boolean;
  downstream_on_stop?: boolean;
}

/** 一版完整编排。开始 / 结束只定义变量，不参与运行。 */
export interface FlowConfig {
  start: { node_id: 'start'; inputs: Record<string, FieldSpec> };
  steps: FlowStep[];
  end: { node_id: 'end'; outputs: Record<string, FieldSpec> };
}

/** 任务定义列表项（`TaskDefinitionItem`）。 */
export interface TaskDefinitionItem {
  id: string;
  scope: string;
  tenant_id?: string | null;
  space_id?: string | null;
  name: string;
  description?: string | null;
  status: DefinitionStatus;
  current_version_no: number;
  created_by?: string | null;
  updated_by?: string | null;
  execution_count: number;
  last_executed_at?: string | null;
  /**
   * 最近一条任务的状态，**未过滤终态**：可能是 `pending` / `running`。
   * 从未执行过为 `null`。
   */
  last_execution_result?: TaskStatus | null;
  creator_name?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TaskDefinitionPage {
  items: TaskDefinitionItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface VersionSummary {
  version_no: number;
  scope?: string | null;
  space_id?: string | null;
  status: 'draft' | 'published';
  published_at?: string | null;
  created_at?: string | null;
}

export interface TaskDefinitionDetail {
  definition: TaskDefinitionItem;
  space_id?: string | null;
  versions: VersionSummary[];
  /** 当前草稿版本号；没有草稿为 null。 */
  draft_version_no?: number | null;
}

export interface VersionDetail {
  id: string;
  definition_id: string;
  scope?: string | null;
  space_id?: string | null;
  version_no: number;
  status: 'draft' | 'published';
  config: FlowConfig;
  published_at?: string | null;
  created_at?: string | null;
}

/** 内置步骤清单的一项。编排里只存 `step_code`，schema 与默认超时都从这里来。 */
export interface TaskCatalogStep {
  id: string;
  step_code: string;
  name: string;
  runner_code: string;
  max_concurrency?: number | null;
  timeout_seconds: number;
  input_schema: Record<string, FieldSpec>;
  output_schema: Record<string, FieldSpec>;
}

export interface DefinitionListParams {
  keyword?: string;
  status?: DefinitionStatus;
  scope?: DefinitionScope;
  /** 主应用当前选中的空间；不传 = 该用户可见的全部空间。 */
  space_id?: string;
  page?: number;
  page_size?: number;
}

export async function listDefinitions(params: DefinitionListParams = {}): Promise<TaskDefinitionPage> {
  return apiClient.get<TaskDefinitionPage>('/task-center/definitions', { params });
}

export interface DefinitionCreateParams {
  name: string;
  description?: string;
  /** 前端固定建空间级定义，空间取自主应用的空间切换器。 */
  space_id: string;
}

export async function createDefinition(params: DefinitionCreateParams): Promise<TaskDefinitionDetail> {
  return apiClient.post<TaskDefinitionDetail>('/task-center/definitions', {
    scope: 'space',
    name: params.name,
    description: params.description,
    space_id: params.space_id,
  });
}

export async function getDefinition(definitionId: string): Promise<TaskDefinitionDetail> {
  return apiClient.get<TaskDefinitionDetail>(`/task-center/definitions/${encodeURIComponent(definitionId)}`);
}

/** 编辑定义：名称必填，描述传 `undefined` 表示不改（后端 PATCH 三态语义）。 */
export interface DefinitionUpdateParams {
  name: string;
  description?: string | null;
}

export async function updateDefinition(
  definitionId: string,
  params: DefinitionUpdateParams,
): Promise<TaskDefinitionDetail> {
  const body: Record<string, unknown> = { name: params.name };
  if (params.description !== undefined) body.description = params.description;
  return apiClient.patch<TaskDefinitionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}`,
    body,
  );
}

export async function deleteDefinition(definitionId: string): Promise<void> {
  await apiClient.delete(`/task-center/definitions/${encodeURIComponent(definitionId)}`);
}

/** 执行定义：只有已发布、且是空间级的定义能跑；`inputs` 按开始节点变量校验。 */
export async function runDefinition(
  definitionId: string,
  inputs: Record<string, unknown> = {},
): Promise<unknown> {
  return apiClient.post(`/task-center/definitions/${encodeURIComponent(definitionId)}/run`, { inputs });
}

export async function copyDefinition(definitionId: string): Promise<TaskDefinitionDetail> {
  return apiClient.post<TaskDefinitionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/copy`,
  );
}

/**
 * 版本列表的形状是 `def.version_list` 的**手拼 dict**（不是 DTO）：
 * 除了 `versions` 还带定义状态与「当前执行用版本号」。
 */
export interface VersionListResult {
  definition_id: string;
  definition_status: DefinitionStatus;
  current_version_no: number;
  latest_version_no: number | null;
  latest_version_status: 'draft' | 'published' | null;
  versions: Array<{
    version_no: number;
    status: 'draft' | 'published';
    created_at?: string | null;
    published_at?: string | null;
  }>;
}

export async function listDefinitionVersions(definitionId: string): Promise<VersionListResult> {
  return apiClient.get<VersionListResult>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/versions`,
  );
}

export async function getDefinitionVersion(
  definitionId: string,
  versionNo: number,
): Promise<VersionDetail> {
  return apiClient.get<VersionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/versions/${versionNo}`,
  );
}

/**
 * 保存编排。后端四分支：最新版是草稿就原地改，否则新建下一版；
 * `status=published` 会顺带发版并推进定义的当前版本号。
 */
export async function saveOrchestration(
  definitionId: string,
  config: FlowConfig,
  status: OrchestrationSaveStatus,
): Promise<VersionDetail> {
  return apiClient.put<VersionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/orchestration`,
    { config, status },
  );
}

/** 发布定义：草稿 / 已停用 → 已发布，前提是已经有已发布的编排版本。 */
export async function publishDefinition(definitionId: string): Promise<TaskDefinitionDetail> {
  return apiClient.post<TaskDefinitionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/publish`,
  );
}

export async function disableDefinition(definitionId: string): Promise<TaskDefinitionDetail> {
  return apiClient.post<TaskDefinitionDetail>(
    `/task-center/definitions/${encodeURIComponent(definitionId)}/disable`,
  );
}

/** 内置步骤清单（当前预置 `parse` / `compile` 两项）。 */
export async function listCatalogSteps(): Promise<TaskCatalogStep[]> {
  return apiClient.get<TaskCatalogStep[]>('/task-center/steps');
}
