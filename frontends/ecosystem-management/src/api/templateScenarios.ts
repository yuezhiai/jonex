import apiClient from './request';

export interface TemplateListResponse<T> {
  items: T[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface TemplateDomain {
  id: string;
  name: string;
  name_en?: string;
  description?: string | null;
  status: 'active' | 'inactive' | 'archived' | string;
  scenario_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TemplateScenario {
  id: string;
  domain_id: string;
  name: string;
  description?: string | null;
  config_json?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TemplateAttribute {
  id: string;
  template_object_id?: string;
  attr_name: string;
  description?: string | null;
  attr_type: string;
  is_primary_key: number | boolean;
  sort_order?: number;
  ontology_code?: string | null;
}

export interface TemplateObject {
  id: string;
  domain_id: string;
  scenario_id: string;
  name: string;
  description?: string | null;
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
  ontology_code?: string | null;
  attributes: TemplateAttribute[];
}

export interface TemplateRelation {
  id: string;
  domain_id: string;
  scenario_id: string;
  name: string;
  description?: string | null;
  source_object_id: string;
  target_object_id: string;
  source_object_name?: string | null;
  target_object_name?: string | null;
  relation_type: string;
  ontology_code?: string | null;
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SaveTemplateScenarioPayload {
  name: string;
  description?: string;
  domain_id: string;
}

export interface SaveTemplateObjectPayload {
  name: string;
  description?: string;
  attributes: Array<{
    attr_name: string;
    description?: string;
    attr_type: string;
    is_primary_key: boolean;
    sort_order?: number;
  }>;
}

export interface SaveTemplateRelationPayload {
  source_object_id: string;
  target_object_id: string;
  name: string;
  description?: string;
  relation_type: string;
}

// ── API（apiClient.get<T> 已解包，返回 data）──

export async function fetchTemplateDomains(offset = 0, limit = 20): Promise<TemplateListResponse<TemplateDomain>> {
  return apiClient.get<TemplateListResponse<TemplateDomain>>('/ecosystem/templates/domains', {
    params: { offset, limit },
  });
}

export async function fetchTemplateScenarios(domainId?: string): Promise<TemplateListResponse<TemplateScenario>> {
  return apiClient.get<TemplateListResponse<TemplateScenario>>('/ecosystem/templates/scenarios', {
    params: { domain_id: domainId || undefined, limit: 100 },
  });
}

export async function createTemplateScenario(data: SaveTemplateScenarioPayload): Promise<TemplateScenario> {
  return apiClient.post<TemplateScenario>('/ecosystem/templates/scenarios', data);
}

export async function updateTemplateScenario(
  scenarioId: string,
  data: Partial<SaveTemplateScenarioPayload>,
): Promise<TemplateScenario> {
  return apiClient.patch<TemplateScenario>(`/ecosystem/templates/scenarios/${scenarioId}`, data);
}

export async function deleteTemplateScenario(scenarioId: string): Promise<void> {
  await apiClient.delete<null>(`/ecosystem/templates/scenarios/${scenarioId}`);
}

export async function fetchTemplateObjects(scenarioId: string): Promise<TemplateListResponse<TemplateObject>> {
  return apiClient.get<TemplateListResponse<TemplateObject>>(`/ecosystem/templates/scenarios/${scenarioId}/objects`, {
    params: { limit: 100 },
  });
}

export async function createTemplateObject(
  scenarioId: string,
  data: SaveTemplateObjectPayload,
): Promise<TemplateObject> {
  return apiClient.post<TemplateObject>(`/ecosystem/templates/scenarios/${scenarioId}/objects`, data);
}

export async function updateTemplateObject(
  objectId: string,
  data: Partial<SaveTemplateObjectPayload>,
): Promise<TemplateObject> {
  return apiClient.patch<TemplateObject>(`/ecosystem/templates/objects/${objectId}`, data);
}

export async function deleteTemplateObject(objectId: string): Promise<void> {
  await apiClient.delete<null>(`/ecosystem/templates/objects/${objectId}`);
}

export async function fetchTemplateRelations(scenarioId: string): Promise<TemplateListResponse<TemplateRelation>> {
  return apiClient.get<TemplateListResponse<TemplateRelation>>(
    `/ecosystem/templates/scenarios/${scenarioId}/relations`,
    { params: { limit: 100 } },
  );
}

export async function createTemplateRelation(
  scenarioId: string,
  data: SaveTemplateRelationPayload,
): Promise<TemplateRelation> {
  return apiClient.post<TemplateRelation>(`/ecosystem/templates/scenarios/${scenarioId}/relations`, data);
}

export async function updateTemplateRelation(
  relationId: string,
  data: Partial<SaveTemplateRelationPayload>,
): Promise<TemplateRelation> {
  return apiClient.patch<TemplateRelation>(`/ecosystem/templates/relations/${relationId}`, data);
}

export async function deleteTemplateRelation(relationId: string): Promise<void> {
  await apiClient.delete<null>(`/ecosystem/templates/relations/${relationId}`);
}

// ── Template Constraints ───────────────────────────────────

export interface TemplateConstraint {
  id: string;
  scenario_id: string;
  name: string;
  target_type: 'object' | 'attribute' | 'relation';
  target_id: string;
  target_label: string;
  constraint_type: 'unique' | 'exists' | 'conditional' | 'range';
  expression?: string | null;
  suggestion?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SaveTemplateConstraintPayload {
  name: string;
  target_type: string;
  target_id: string;
  constraint_type: string;
  expression?: string;
  suggestion?: string;
}

export async function fetchTemplateConstraints(scenarioId: string): Promise<TemplateListResponse<TemplateConstraint>> {
  return apiClient.get<TemplateListResponse<TemplateConstraint>>(
    `/ecosystem/templates/scenarios/${scenarioId}/constraints`,
    { params: { limit: 100 } },
  );
}

export async function createTemplateConstraint(
  scenarioId: string,
  data: SaveTemplateConstraintPayload,
): Promise<TemplateConstraint> {
  return apiClient.post<TemplateConstraint>(`/ecosystem/templates/scenarios/${scenarioId}/constraints`, data);
}

export async function updateTemplateConstraint(
  constraintId: string,
  data: Partial<SaveTemplateConstraintPayload>,
): Promise<TemplateConstraint> {
  return apiClient.patch<TemplateConstraint>(`/ecosystem/templates/constraints/${constraintId}`, data);
}

export async function deleteTemplateConstraint(constraintId: string): Promise<void> {
  await apiClient.delete<null>(`/ecosystem/templates/constraints/${constraintId}`);
}

// ── YAML Import/Export ───────────────────────────────────

export interface YamlImportSummary {
  entities: { create: number; update: number; skip: number }
  attributes: { create: number; update: number; skip: number }
  relations: { create: number; update: number; skip: number }
  constraints: { create: number; update: number; skip: number }
}

export interface YamlImportResult {
  dry_run: boolean
  mode: string
  summary: YamlImportSummary
  warnings: string[]
  errors: string[]
}

export async function exportScenarioOntologyYaml(
  scenarioId: string,
): Promise<{ filename: string; yaml_text: string; warnings: string[] }> {
  return apiClient.get<{ filename: string; yaml_text: string; warnings: string[] }>(
    `/ecosystem/templates/scenarios/${scenarioId}/ontology-yaml/export`,
  )
}

export async function importScenarioOntologyYaml(
  scenarioId: string,
  file: File,
  dryRun: boolean,
): Promise<YamlImportResult> {
  const fd = new FormData()
  fd.append('file', file)
  return apiClient.post<YamlImportResult>(
    `/ecosystem/templates/scenarios/${scenarioId}/ontology-yaml/import?dry_run=${dryRun}&mode=merge`,
    fd,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
}
