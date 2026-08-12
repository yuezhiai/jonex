/**
 * 文档处理状态 —— 线性 phase 派生（唯一事实来源）
 *
 * 后端两个字段 (status, ontology_status) → 单一线性状态。前端所有展示 / 菜单 / 筛选
 * 都基于此，避免规则散落。与后端 document_service._PHASE_PREDICATE、设计文档
 * docs/document-status-display-design.md §2.2 / §8.1 保持同源同义。
 */

export type DocPhase =
  | 'pending_parse'
  | 'parsing'
  | 'ingesting'
  | 'parse_failed'
  | 'pending_compile'
  | 'compiling'
  | 'compiled'
  | 'compile_failed'
  | 'deleting'; // 正交删除态，单独处理，不进主线

/** 徽章图标语义键（由 DocumentStatusBadge 映射到具体 antd 图标）。 */
export type PhaseIcon = 'clock' | 'spinner' | 'check' | 'error' | 'warning' | 'hourglass';

export interface PhaseDisplay {
  label: string;
  color: string;
  icon: PhaseIcon;
  /** 是否已可搜索（待编译起为 true）。 */
  searchable: boolean;
  canReparse: boolean;
  canRecompile: boolean;
  canDelete: boolean;
  /** 副提示，如「已可搜索」。 */
  hint?: string;
}

/**
 * 后端两字段 → 单一线性 phase。传入的是后端原始值（status / ontology_status）。
 * openkb 文档额外传 kbType + llm_wiki_compile_status：其 ontology_status 被强制
 * ready 表示「不需要本体抽取」，不能当「编译完成」——openkb 编译状态以
 * llm_wiki 为准（kbType 显式判断，避免依赖「llmWikiCompileStatus 有值」的隐式约定）。
 * 返回 null 表示列表不展示（如 deleted 软删除）。
 */
export function deriveDocPhase(
  status: string | undefined | null,
  ontologyStatus: string | undefined | null,
  llmWikiCompileStatus?: string | null,
  kbType?: string | null,
): DocPhase | null {
  switch (status) {
    case 'pending':
      return 'pending_parse';
    case 'parsing':
      return 'parsing';
    case 'ingesting':
      return 'ingesting';
    case 'failed':
      return 'parse_failed';
    case 'deleting':
      return 'deleting';
    case 'deleted':
      return null;
    case 'ready':
      // [jonex] openkb 文档：llm_wiki_compile_status 是编译状态唯一事实来源
      // （NULL/compiling/compiled/stale/failed；ontology_status 恒 ready）
      if (kbType === 'openkb' && llmWikiCompileStatus) {
        switch (llmWikiCompileStatus) {
          case 'compiled':
            return 'compiled';
          case 'compiling':
            return 'compiling';
          case 'failed':
            return 'compile_failed';
          case 'stale':
            return 'pending_compile'; // 已过期，需重新编译
          default:
            return 'pending_compile'; // NULL：未编译
        }
      }
      switch (ontologyStatus) {
        case 'pending':
          return 'pending_compile';
        case 'extracting':
          return 'compiling';
        case 'ready':
          return 'compiled';
        case 'failed':
          return 'compile_failed';
        default:
          return 'pending_compile';
      }
    default:
      return null;
  }
}

export const PHASE_DISPLAY: Record<DocPhase, PhaseDisplay> = {
  pending_parse: {
    label: 'docPhase.pendingParse',
    color: '#94a3b8',
    icon: 'clock',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  parsing: {
    label: 'docPhase.parsing',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  ingesting: {
    label: 'docPhase.ingesting',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  parse_failed: {
    label: 'docPhase.parseFailed',
    color: '#ef4444',
    icon: 'error',
    searchable: false,
    canReparse: true,
    canRecompile: false,
    canDelete: true,
  },
  pending_compile: {
    label: 'docPhase.pendingCompile',
    color: '#64748b',
    icon: 'hourglass',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
    hint: 'docPhase.searchable',
  },
  compiling: {
    label: 'docPhase.compiling',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: true,
    canReparse: false,
    canRecompile: false,
    canDelete: true,
    hint: 'docPhase.searchable',
  },
  compiled: {
    label: 'docPhase.compiled',
    color: '#10b981',
    icon: 'check',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
  },
  compile_failed: {
    label: 'docPhase.compileFailed',
    color: '#f59e0b',
    icon: 'warning',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
    hint: 'docPhase.stillSearchable',
  },
  deleting: {
    label: 'docPhase.deleting',
    color: '#94a3b8',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
};

/** 列表筛选顺序（不含 deleting；它是正交删除态）。 */
export const FILTER_PHASES: DocPhase[] = [
  'pending_parse',
  'parsing',
  'ingesting',
  'parse_failed',
  'pending_compile',
  'compiling',
  'compiled',
  'compile_failed',
];
