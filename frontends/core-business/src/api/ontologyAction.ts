// 本体动作试执行：执行前预览（知识库，读口径）+ 执行（执行器，按知识库写权限判定调用人）。
// 执行会真实调用对方接口；先 dry_run 拿到组装好的入参，再确认后真正执行。
import { postData } from './request';

export type ExecutionVerdict = 'allow' | 'confirm' | 'manual' | 'refuse';

export interface ExecutionChecklistItem {
  kind: 'authorization' | 'precondition' | string;
  items: string[];
}

export interface ExecutionDecision {
  verdict: ExecutionVerdict;
  executable: boolean;
  reasons: { code: string; message: string }[];
  checklist: ExecutionChecklistItem[];
}

export interface ExecutionPlan {
  knowledge_base_id: string;
  action: {
    name: string;
    action_type?: string | null;
    step_no?: number | null;
    definition?: string;
    doc_ids?: string[];
  } | null;
  contract: Record<string, { status: 'present' | 'missing' | 'explicit_none' | string; value?: unknown }>;
  decision: ExecutionDecision;
  binding: {
    binding_id: string;
    tool_source: 'platform' | 'kb';
    tool_name: string;
    tool_label: string | null;
    binding_enabled: boolean;
    tool_available: boolean;
    param_mapping: Record<string, unknown>;
    input_schema: Record<string, any>;
    read_only?: boolean;
  } | null;
  parameters: {
    resolved: Record<string, unknown>;
    input_refs: Record<string, string>;
    unresolved_attrs: string[];
  };
}

export type ExecuteStatus =
  | 'executed'
  | 'planned'
  | 'blocked'
  | 'manual'
  | 'needs_confirmation'
  | 'invalid_input'
  | 'no_runtime'
  | 'failed';

export interface ExecuteResult {
  status: ExecuteStatus;
  knowledge_base_id: string;
  action: ExecutionPlan['action'];
  decision: ExecutionDecision;
  tool_name?: string | null;
  tool_source?: 'platform' | 'kb';
  arguments?: Record<string, unknown> | null;
  arguments_keys?: string[];
  output?: unknown;
  output_truncated?: boolean;
  http_status?: number | null;
  error?: string | null;
  missing_inputs?: string[];
  pending_confirmations?: ExecutionChecklistItem[];
  post_actions?: { follow_ups: string[]; failure_policy: string };
  dry_run: boolean;
  duration_ms?: number;
}

export interface ExecuteInput {
  knowledgeBaseId: string;
  actionName: string;
  inputs: Record<string, unknown>;
  confirmations: Record<string, boolean>;
  dryRun: boolean;
}

/** 执行前预览：契约状态、判定、绑定工具与参数计划（不执行）。 */
export function resolveActionExecution(kbId: string, actionName: string): Promise<ExecutionPlan> {
  return postData<ExecutionPlan>('/knowledge-base/ontology/actions/resolve-execution', {
    knowledge_base_id: kbId,
    action_name: actionName,
  });
}

/** 执行（dryRun=true 只组装入参不调用）。审计里入参只记键名。 */
export function executeOntologyAction(input: ExecuteInput): Promise<ExecuteResult> {
  return postData<ExecuteResult>('/ecosystem/ontology-actions/execute', {
    knowledge_base_id: input.knowledgeBaseId,
    action_name: input.actionName,
    inputs: input.inputs,
    confirmations: input.confirmations,
    dry_run: input.dryRun,
  });
}
