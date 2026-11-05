import { getData, request } from './request';
export type ScenarioStrategy = string;
export interface WikiScenario {
  id: string; version: string; label: string; description: string;
  kinds: string[]; kind_labels: Record<string, string>;
  enabled: boolean; status: 'disabled' | 'queued' | 'running' | 'ready' | 'failed' | 'stale';
  strategy?: ScenarioStrategy; strategies: ScenarioStrategy[]; document_ids?: string[]; operations: string[]; available_operations: string[];
  base_revision?: string; result_revision?: string; error?: string;
}
export interface WikiScenarioState { version: number; base_revision: string | null; items: WikiScenario[]; documents: { document_id: string; label: string }[] }
export interface ScenarioObject {
  object_id: string; kind: string; label: string; interpretation: string;
  missing: string[]; uncertainties: string[]; review_status: string; document_ids: string[];
  origin?: { kind: string; actor?: string; model?: string };
  fragments: { document_id: string; source_name?: string; text?: string; modality: string;
    locator: Record<string, unknown>; source_sha256: string }[];
}
export interface ScenarioResources {
  base_revision: string; revision: string; review_version: number; total: number; has_more: boolean;
  items: ScenarioObject[]; coverage: { processed: number; total: number; semantic_completeness: string; rejected_candidates?: number };
}
const url = (kbId: string) => ({ params: { knowledge_base_id: kbId } });
export const listWikiScenarios = (kbId: string) =>
  getData<WikiScenarioState>(request.get('/knowledge-base/wiki/scenarios', url(kbId)));
export const configureWikiScenario = (kbId: string, scenarioId: string, options: {
  expected_version: number; enabled: boolean; strategy: ScenarioStrategy; document_ids: string[];
}) => getData<WikiScenarioState>(request.put('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId), options, url(kbId)));
export const runWikiScenario = (kbId: string, scenarioId: string) =>
  getData<{ job_id: string; status: string }>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/interpret', {}, url(kbId)));
export const readWikiScenario = (kbId: string, scenarioId: string, options: object = {}) =>
  getData<ScenarioResources>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/resources', options, url(kbId)));
export const reviewWikiScenario = (kbId: string, scenarioId: string, options: {
  expected_version: number; object_id: string; status: 'accepted' | 'rejected'; note?: string;
}) => getData(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/review', options, url(kbId)));
export interface ScenarioSelection {
  selection_id: string; base_revision: string; scenario_id: string; scenario_version: string;
  result_revision: string; review_version: number; objects: ScenarioObject[];
}
export const selectWikiScenarioResources = (kbId: string, scenarioId: string, options: {
  object_ids: string[]; expected_revision: string; expected_review_version: number;
}) => getData<ScenarioSelection>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/selection', options, url(kbId)));
export interface ScenarioMaterial {
  unit_id: string; document_id: string; source_name: string; source_sha256: string; modality: string;
  locator: Record<string, unknown>; text?: string; preview?: string;
}
export const listWikiScenarioMaterials = (kbId: string, scenarioId: string, offset = 0) =>
  getData<{ items: ScenarioMaterial[]; total: number; has_more: boolean }>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/materials', { offset, limit: 50 }, url(kbId)));
export const readWikiScenarioMaterial = (kbId: string, scenarioId: string, unitId: string) =>
  getData<{ unit: ScenarioMaterial; base_revision: string }>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/material', { unit_id: unitId }, url(kbId)));
export const annotateWikiScenario = (kbId: string, scenarioId: string, options: {
  expected_version: number; kind: string; label: string; interpretation: string;
  fragments: { unit_id: string; quote: string }[];
}) => getData<{ revision: string; object_id: string }>(request.post('/knowledge-base/wiki/scenarios/' + encodeURIComponent(scenarioId) + '/annotate', options, url(kbId)));
