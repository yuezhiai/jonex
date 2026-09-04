import axios from 'axios';
import { readAccessToken } from '@jonex/shell-sdk';

const templateApi = axios.create({
  baseURL: (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1',
  timeout: 30000,
});

templateApi.interceptors.request.use((config) => {
  const token = readAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

export interface TemplateDomain {
  id: string;
  name: string;
  description?: string | null;
  status: string;
}

export interface TemplateScenario {
  id: string;
  domain_id: string;
  name: string;
  description?: string | null;
}

export interface TemplateAttribute {
  id: string;
  attr_name: string;
  description?: string | null;
  attr_type: string;
  is_primary_key: number | boolean;
  sort_order?: number;
}

export interface TemplateObject {
  id: string;
  scenario_id: string;
  name: string;
  description?: string | null;
  attributes: TemplateAttribute[];
}

export interface TemplateRelation {
  id: string;
  scenario_id: string;
  name: string;
  description?: string | null;
  source_object_id: string;
  target_object_id: string;
  source_object_name?: string | null;
  target_object_name?: string | null;
  relation_type: string;
}

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
}

interface ListResp<T> {
  items: T[];
  total: number;
}

export interface CompiledTemplatePreview {
  entity_types: Array<{
    name: string;
    display_name?: string;
    aliases?: string[];
    attributes?: Array<{
      name: string;
      display_name?: string;
      type: string;
      required?: boolean;
    }>;
  }>;
  relation_types: Array<{
    name: string;
    display_name?: string;
    aliases?: string[];
    source: string;
    target: string;
    cardinality?: string;
  }>;
  source_version: number;
  source_hash?: string | null;
}

// 兼容 { code, message, data } 与 { success, message, data } 两种 envelope
function unwrap<T>(body: any): T {
  if (body && typeof body === 'object' && 'data' in body) {
    if (body.success === false || (typeof body.code === 'number' && body.code !== 0)) {
      throw new Error(body.message || 'Request failed');
    }
    return body.data as T;
  }
  return body as T;
}

export async function fetchTemplateDomains(): Promise<ListResp<TemplateDomain>> {
  const resp = await templateApi.get('/ecosystem/templates/domains', { params: { limit: 100, status: 'active' } });
  return unwrap<ListResp<TemplateDomain>>(resp.data);
}

export async function fetchTemplateScenarios(domainId?: string): Promise<ListResp<TemplateScenario>> {
  const resp = await templateApi.get('/ecosystem/templates/scenarios', {
    params: { domain_id: domainId || undefined, limit: 100 },
  });
  return unwrap<ListResp<TemplateScenario>>(resp.data);
}

export async function fetchTemplateObjects(scenarioId: string): Promise<ListResp<TemplateObject>> {
  const resp = await templateApi.get(`/ecosystem/templates/scenarios/${scenarioId}/objects`, {
    params: { limit: 100 },
  });
  return unwrap<ListResp<TemplateObject>>(resp.data);
}

export async function fetchTemplateRelations(scenarioId: string): Promise<ListResp<TemplateRelation>> {
  const resp = await templateApi.get(`/ecosystem/templates/scenarios/${scenarioId}/relations`, {
    params: { limit: 100 },
  });
  return unwrap<ListResp<TemplateRelation>>(resp.data);
}

export async function fetchTemplateConstraints(scenarioId: string): Promise<ListResp<TemplateConstraint>> {
  const resp = await templateApi.get(`/ecosystem/templates/scenarios/${scenarioId}/constraints`, {
    params: { limit: 100 },
  });
  return unwrap<ListResp<TemplateConstraint>>(resp.data);
}

export async function fetchTemplateCompilePreview(scenarioId: string): Promise<CompiledTemplatePreview> {
  const resp = await templateApi.get(`/ecosystem/templates/scenarios/${scenarioId}/compile-preview`);
  return unwrap<CompiledTemplatePreview>(resp.data);
}
