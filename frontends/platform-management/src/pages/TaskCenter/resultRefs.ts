import type { SubtaskRef } from '../../api/taskCenter';

/**
 * 子任务引用（`input_refs` / `output_refs`）→ 页面展示与跳转目标。
 *
 * 任务中心**只搬运引用、不放内容**（设计文档 §4.4）：`kind` 说「这是什么」，
 * `params` 说「去哪儿找它」。所以这里做两件事：
 *
 * - `kind` → 文案 key（中文名在 locales 里，不把中文写进映射表）；
 * - `kind` + `params` → **core-business 的页面路由**（不是运行方 API 路径 —— 直接跳
 *   API 会丢 `Authorization` 头，权限也在运行方那侧判）。
 *
 * 映射**只认本运行方（knowledge_base）的那几个 kind**。别的运行方的 kind 原样透出，
 * 不给跳转按钮 —— 宁可不显示，也不给一个点进去 404 的链接（与后端
 * `_artifact_refs` 的收尾口径一致）。
 */

/** 任务来源的库页面根路径（core-business 子应用）。 */
const KB_PAGE_BASE = '/apps/core-business/domain-knowledge';

/** `kind` → 中文名的 i18n key。 */
const KIND_LABEL_KEY: Record<string, string> = {
  'knowledge_base.document': 'taskCenter.execution.kind.document',
  'knowledge_base.chunks': 'taskCenter.execution.kind.chunks',
  'knowledge_base.ontology': 'taskCenter.execution.kind.ontology',
  'knowledge_base.wiki': 'taskCenter.execution.kind.wiki',
};

/** 产物 `kind` → 产物名 / 查看动作文案的 i18n key。 */
const ARTIFACT_KEY: Record<string, { title: string; action: string }> = {
  'knowledge_base.chunks': {
    title: 'taskCenter.execution.artifact.chunks',
    action: 'taskCenter.execution.viewChunks',
  },
  'knowledge_base.ontology': {
    title: 'taskCenter.execution.artifact.ontology',
    action: 'taskCenter.execution.viewOntology',
  },
  'knowledge_base.wiki': {
    title: 'taskCenter.execution.artifact.wiki',
    action: 'taskCenter.execution.viewWiki',
  },
};

export interface RefView {
  kind: string;
  /** 只保留非空的标量参数：对象 / 数组 / null 不进 UI，也拼不进 URL。 */
  params: Record<string, string>;
  /** `kind` 的中文名 key；未知运行方的 kind 为 `null`，由调用方原样显示 `kind`。 */
  labelKey: string | null;
  /** 跳转目标；`kind` 不认识、或关键参数缺失时为 `null`。 */
  path: string | null;
  /** 产物标题 / 查看动作的 key；仅 output 侧的已知 kind 有。 */
  artifact: { title: string; action: string } | null;
}

function scalarParams(params: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(params ?? {})) {
    // 协议层要求 params 的值是**非空标量**；这里再收一道，防老报文 / 手填数据里混进数组
    if (typeof value === 'string' && value) out[key] = value;
    else if (typeof value === 'number' && Number.isFinite(value)) out[key] = String(value);
  }
  return out;
}

/**
 * `kind` + `params` → core-business 页面路径。
 *
 * 三条落点（与 `capabilities/knowledge_base/services/task_watch_service.py` 的
 * `_artifact_refs` 一一对应）：
 *
 * - `chunks` / `wiki` → 文档解析结果页（切片与 Wiki 页都在那一页的编译结果里）；
 * - `ontology` → 知识图谱页（按库，不按文档）；
 * - 其它 → `null`。
 */
export function artifactPath(kind: string, params: Record<string, string>): string | null {
  const kbId = params.knowledge_base_id;
  const docId = params.document_id;
  if (!kbId) return null;
  switch (kind) {
    case 'knowledge_base.chunks':
    case 'knowledge_base.wiki':
      // 缺 document_id 时无法定位到具体文档：退化成「整库」而不是拼一个缺段的死链
      return docId
        ? `${KB_PAGE_BASE}/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(docId)}/result`
        : null;
    case 'knowledge_base.ontology':
      return `${KB_PAGE_BASE}/${encodeURIComponent(kbId)}/graph`;
    default:
      return null;
  }
}

/** 把一条后端引用整理成页面能直接渲染的形状。 */
export function describeRef(ref: SubtaskRef, side: 'input' | 'output' = 'output'): RefView {
  const params = scalarParams(ref.params);
  return {
    kind: ref.kind,
    params,
    labelKey: KIND_LABEL_KEY[ref.kind] ?? null,
    path: artifactPath(ref.kind, params),
    artifact: side === 'output' ? (ARTIFACT_KEY[ref.kind] ?? null) : null,
  };
}

/**
 * 输入文件的名字：运行方给的是 `document_name`，查不到文档时它已回落成 `document_id`
 * （见后端 `build_input_refs`），所以这里只做「有名字显示名字、没名字就算了」。
 */
export function inputFileName(view: RefView): string | null {
  if (view.kind !== 'knowledge_base.document') return null;
  return view.params.document_name || view.params.document_id || null;
}
