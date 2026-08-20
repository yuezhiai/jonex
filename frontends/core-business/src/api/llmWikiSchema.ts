/**
 * [jonex] LLM-Wiki Schema 编译设置 API（方案 llmwiki-schema-settings-execution-plan §9）。
 *
 * HTTP 风格对齐现有本体 API 的查询参数式（Gateway → Sidecar → knowledge_base）。
 */
import { getData, request } from './request';
import type {
  ImportLlmWikiSchemaPayload,
  LlmWikiSchema,
  RecompileOutdatedResult,
  SaveLlmWikiSchemaPayload,
} from '@/types/domainKnowledge';

/** 获取 active LLM-Wiki Schema（缺省后端自动创建默认）。 */
export function getLlmWikiSchema(kbId: string): Promise<LlmWikiSchema> {
  return getData<LlmWikiSchema>(
    request.get('/knowledge-base/llm-wiki/schema', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

/** 保存（CAS：必须带 expected_schema_version）。 */
export function saveLlmWikiSchema(payload: SaveLlmWikiSchemaPayload): Promise<LlmWikiSchema> {
  return getData<LlmWikiSchema>(
    request.put('/knowledge-base/llm-wiki/schema', payload),
  );
}

/** 同步到 OpenKB 引擎（sync_status=apply_failed 时手动补偿）。 */
export function applyLlmWikiSchema(kbId: string): Promise<{ applied: boolean; schema_version: number }> {
  return getData(
    request.post('/knowledge-base/llm-wiki/schema/apply', null, {
      params: { knowledge_base_id: kbId },
    }),
  );
}

/** 导出 YAML。 */
export function exportLlmWikiSchemaYaml(kbId: string): Promise<{ yaml_text: string }> {
  return getData(
    request.get('/knowledge-base/llm-wiki/schema/export', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

/** 导入 YAML（dry_run 校验 / 非 dry-run 全量替换，走同一 CAS）。 */
export function importLlmWikiSchemaYaml(payload: ImportLlmWikiSchemaPayload): Promise<LlmWikiSchema | { valid: boolean }> {
  return getData(
    request.post('/knowledge-base/llm-wiki/schema/import', payload),
  );
}

/** 全库重编过期文档（一次性提交，返回匹配/提交/跳过/失败数）。 */
export function recompileOutdatedDocuments(kbId: string): Promise<RecompileOutdatedResult> {
  return getData<RecompileOutdatedResult>(
    request.post('/knowledge-base/llm-wiki/schema/recompile-outdated', {
      knowledge_base_id: kbId,
      only_outdated: true,
    }),
  );
}
