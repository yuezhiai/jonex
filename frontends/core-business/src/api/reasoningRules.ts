/**
 * 推理规则（T6/T8）。
 *
 * 后端：`/api/v1/knowledge-base/ontology/reasoning-rules/*`。
 * 规则 AST 为权威；text 仅展示。所有写操作带 `expected_content_revision`（如有）。
 */
import { request, getData, postData, putData, deleteData } from './request';

export type RuleStatus = 'proposed' | 'active' | 'rejected';
export type RuleSeverity = 'Violation' | 'Warning' | 'Info';

export interface RuleAtom {
  kind: 'rel' | 'type';
  s: string;
  o?: string;
  p?: string;
  cls?: string;
}

export interface RuleIssue {
  code: string;
  severity: RuleSeverity;
  target?: { kind?: string; id?: string; field?: string };
  params?: Record<string, unknown>;
}

export interface ReasoningRule {
  rule_key: string;
  knowledge_base_id?: string;
  name: string;
  text: string;
  premises: RuleAtom[];
  conclusion: RuleAtom;
  status: RuleStatus;
  enabled: boolean;
  origin: 'studio' | 'platform' | 'imported';
  module?: string | null;
  content_revision: number;
  description?: string | null;
  updated_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  issues?: RuleIssue[];
}

export interface RuleSavePayload {
  rule_key?: string;
  name: string;
  text: string;
  status: RuleStatus;
  enabled?: boolean;
  description?: string;
  expected_content_revision?: number;
}

export interface RuleListResult {
  items: ReasoningRule[];
  total: number;
}

export interface AxiomRef {
  kind: 'entity' | 'relation' | 'attribute';
  ref: string;
  owner?: string;
}

export interface OntologyAxiom {
  axiom_key: string;
  kind: string;
  subject: AxiomRef;
  object?: AxiomRef | null;
  chain?: { kind: 'relation'; ref: string }[];
  facets?: { kind: string; value: string | number }[];
  status: RuleStatus;
  source?: string;
  origin?: string;
  module?: string | null;
  content_revision?: number;
  description?: string | null;
  updated_at?: string | null;
  issues?: RuleIssue[];
}

export interface AxiomViewChange {
  key: string;
  added: string[];
  removed: string[];
}

export interface AxiomRuleImpact {
  rule_key: string;
  name?: string;
  status?: RuleStatus;
  enabled?: boolean;
  new_issues: RuleIssue[];
  resolved_issues: RuleIssue[];
}

export interface AxiomStatusPreview {
  axiom_key: string;
  from_status: RuleStatus;
  to_status: RuleStatus;
  unchanged?: boolean;
  axiom: OntologyAxiom;
  impact: {
    entities: AxiomViewChange[];
    relations: AxiomViewChange[];
    attributes: AxiomViewChange[];
    characteristics: AxiomViewChange[];
    summary: {
      entities: number;
      relations: number;
      attributes: number;
      characteristics: number;
    };
  };
  affected_rules: AxiomRuleImpact[];
  new_axiom_issues?: RuleIssue[];
  has_violation: boolean;
  notes: string[];
}

export interface RuleValidateResult {
  contract_version: number;
  rules: unknown[];
  issues: RuleIssue[];
  has_violation: boolean;
  warnings: string[];
}

export interface PreviewDerived {
  rule_key?: string;
  s?: string;
  p?: string;
  o?: string;
  s_type?: string;
  o_type?: string;
  already_asserted?: boolean;
  premise?: {
    edges?: { s?: string; p?: string; o?: string; docs?: string[] }[];
    doc_ids?: string[];
  };
}

export interface PreviewResult {
  contract_version: number;
  results: {
    rule_key?: string;
    status?: string;
    derived?: PreviewDerived[];
    count?: number;
    reason?: string;
    layer?: number;
  }[];
  skipped?: { rule_key?: string; reason?: string }[];
  issues?: RuleIssue[];
}

export interface RuleImportResult {
  dry_run: boolean;
  contract_version: number;
  summary: Record<string, number>;
  plan?: { rule_key: string; action: string }[];
  plan_hash?: string;
  issues?: RuleIssue[];
  warnings?: string[];
}

const BASE = '/knowledge-base/ontology/reasoning-rules';

export async function listReasoningRules(kbId: string): Promise<RuleListResult> {
  return getData<RuleListResult>(
    request.get(BASE, { params: { knowledge_base_id: kbId } }),
  );
}

export async function listOntologyAxioms(
  kbId: string,
): Promise<{ items: OntologyAxiom[]; total: number }> {
  return getData<{ items: OntologyAxiom[]; total: number }>(
    request.get('/knowledge-base/ontology/axioms', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export async function previewAxiomStatus(
  kbId: string,
  axiomKey: string,
  status: 'active' | 'proposed',
): Promise<AxiomStatusPreview> {
  return postData<AxiomStatusPreview>(
    `/knowledge-base/ontology/axioms/${encodeURIComponent(axiomKey)}/status/preview`,
    { knowledge_base_id: kbId, status },
  );
}

export async function setAxiomStatus(
  kbId: string,
  axiomKey: string,
  status: 'active' | 'proposed',
  options: { force?: boolean; expectedStatus?: string } = {},
): Promise<AxiomStatusPreview & { applied: boolean }> {
  return putData<AxiomStatusPreview & { applied: boolean }>(
    `/knowledge-base/ontology/axioms/${encodeURIComponent(axiomKey)}/status`,
    {
      knowledge_base_id: kbId,
      status,
      force: options.force ?? false,
      expected_status: options.expectedStatus,
    },
  );
}

export async function getReasoningRule(
  kbId: string,
  ruleKey: string,
): Promise<ReasoningRule> {
  return getData<ReasoningRule>(
    request.get(`${BASE}/${encodeURIComponent(ruleKey)}`, {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export async function validateReasoningRules(
  kbId: string,
  rules: RuleSavePayload[],
): Promise<RuleValidateResult> {
  // postData/putData/deleteData 已由 createRequest 解包，不能再套 getData。
  return postData<RuleValidateResult>(
    BASE + '/validate', { knowledge_base_id: kbId, rules },
  );
}

export async function createReasoningRule(
  kbId: string,
  payload: RuleSavePayload,
): Promise<ReasoningRule> {
  return postData<ReasoningRule>(
    BASE, { knowledge_base_id: kbId, ...payload },
  );
}

export async function updateReasoningRule(
  kbId: string,
  ruleKey: string,
  payload: RuleSavePayload,
): Promise<ReasoningRule> {
  return putData<ReasoningRule>(
    `${BASE}/${encodeURIComponent(ruleKey)}`, {
      knowledge_base_id: kbId,
      ...payload,
    },
  );
}

export async function deleteReasoningRule(
  kbId: string,
  ruleKey: string,
): Promise<{ deleted: boolean }> {
  return deleteData<{ deleted: boolean }>(
    `${BASE}/${encodeURIComponent(ruleKey)}`, undefined, {
      params: { knowledge_base_id: kbId },
    },
  );
}

export async function setReasoningRuleEnabled(
  kbId: string,
  ruleKey: string,
  enabled: boolean,
): Promise<ReasoningRule> {
  return postData<ReasoningRule>(
    `${BASE}/${encodeURIComponent(ruleKey)}/enabled`, {
      knowledge_base_id: kbId,
      enabled,
    },
  );
}

export async function previewReasoning(
  kbId: string,
  options: {
    ruleKeys?: string[];
    rule?: RuleSavePayload;
    anchorEntity?: string;
    limit?: number;
  } = {},
): Promise<PreviewResult> {
  return postData<PreviewResult>(
    BASE + '/preview', {
      knowledge_base_id: kbId,
      rule_keys: options.ruleKeys,
      rule: options.rule,
      anchor_entity: options.anchorEntity,
      limit: options.limit ?? 50,
    },
  );
}

export async function exportReasoningRules(
  kbId: string,
): Promise<{ filename: string; yaml_text: string; count: number }> {
  return getData<{ filename: string; yaml_text: string; count: number }>(
    request.get(BASE + '/export', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export async function importReasoningRules(
  kbId: string,
  yamlText: string,
  options: {
    dryRun?: boolean;
    mode?: 'merge' | 'replace';
    removedRuleKeys?: string[];
    planHash?: string;
    overrideEnabled?: boolean;
  } = {},
): Promise<RuleImportResult> {
  return postData<RuleImportResult>(
    BASE + '/import', {
      knowledge_base_id: kbId,
      yaml_text: yamlText,
      dry_run: options.dryRun ?? true,
      mode: options.mode ?? 'merge',
      removed_rule_keys: options.removedRuleKeys,
      plan_hash: options.planHash,
      override_enabled: options.overrideEnabled ?? false,
    },
  );
}
