/**
 * 能力 ID → 人类可读释义。
 *
 * 能力 ID 里含点（`business.task_center.v1`），**不能直接当 i18next 的 key**：
 * 默认 `keySeparator` 是 `.`，会把它拆成多层路径而查不到。这里给每条能力一个安全的
 * 扁平 key（`platformMonitor.capability.business_task_center_v1`），页面统一经
 * `capabilityLabel()` 取值；不在表里的能力 / 模型名 / MCP 工具名原样回退（C4）。
 *
 * keyword 同时按中英文释义与原始对象标识做大小写不敏感子串匹配，
 * 命中的原始值交给调用记录页作为 `target_any` 下发。
 */
import type { TFunction } from 'i18next';
import type { PmCallProtocol } from './types';

/** 12 条已注册能力（顺序即词条维护顺序，不作为页面排序依据） */
export const CAPABILITY_IDS = [
  'business.business_domain.v1',
  'business.knowledge_base.v1',
  'business.task_center.v1',
  'business.data_governance.v1',
  'business.platform.v1',
  'atomic.rag.lightrag.v1',
  'atomic.openkb.v1',
  'atomic.jonex_wiki.v1',
  'atomic.ontology_compile.v1',
  'atomic.llm.qwen.v1',
  'atomic.audio.asr.v1',
  'atomic.vector.milvus.v1',
] as const;

/** 能力 ID → 安全 i18n key（不含能力 ID 自己的点） */
export const CAPABILITY_I18N_KEY: Record<string, string> = {
  'business.business_domain.v1': 'platformMonitor.capability.business_business_domain_v1',
  'business.knowledge_base.v1': 'platformMonitor.capability.business_knowledge_base_v1',
  'business.task_center.v1': 'platformMonitor.capability.business_task_center_v1',
  'business.data_governance.v1': 'platformMonitor.capability.business_data_governance_v1',
  'business.platform.v1': 'platformMonitor.capability.business_platform_v1',
  'atomic.rag.lightrag.v1': 'platformMonitor.capability.atomic_rag_lightrag_v1',
  'atomic.openkb.v1': 'platformMonitor.capability.atomic_openkb_v1',
  'atomic.jonex_wiki.v1': 'platformMonitor.capability.atomic_jonex_wiki_v1',
  'atomic.ontology_compile.v1': 'platformMonitor.capability.atomic_ontology_compile_v1',
  'atomic.llm.qwen.v1': 'platformMonitor.capability.atomic_llm_qwen_v1',
  'atomic.audio.asr.v1': 'platformMonitor.capability.atomic_audio_asr_v1',
  'atomic.vector.milvus.v1': 'platformMonitor.capability.atomic_vector_milvus_v1',
};

/**
 * 能力 ID → 当前语言释义。
 *
 * 未知值（新增能力、模型名、MCP 工具名）回退原值；`null` / 空串才是「没有值」，落 `-`。
 * `defaultValue` 是双保险：即使某条词条漏补，也不会把 i18n key 名甩到页面上。
 */
export function capabilityLabel(t: TFunction, value: string | null | undefined): string {
  if (!value) return '-';
  const key = CAPABILITY_I18N_KEY[value];
  return key ? t(key, { defaultValue: value }) : value;
}

/** keyword → 命中的能力 ID（中文名 / 英文名，大小写不敏感子串） */
export function matchCapabilityIds(keyword: string, t: TFunction): string[] {
  const needle = keyword.trim().toLowerCase();
  if (!needle) return [];

  return CAPABILITY_IDS.filter((id) => {
    const key = CAPABILITY_I18N_KEY[id];
    const zh = String(t(key, { lng: 'zh', defaultValue: '' })).toLowerCase();
    const en = String(t(key, { lng: 'en', defaultValue: '' })).toLowerCase();
    return zh.includes(needle) || en.includes(needle);
  });
}

/**
 * keyword 展开成要下发给后端的 `target_any[]`。
 *
 * 两路合并：能力释义命中（中英文）→ 原始能力 ID；`meta.callTargets` 原值（模型名 /
 * MCP 工具名）大小写不敏感子串命中 → 原始值。返回去重后的原始标识，传输仍走原值。
 */
export function expandKeywordTargets(
  keyword: string,
  callTargets: readonly string[],
  t: TFunction,
): string[] {
  const matched = new Set<string>(matchCapabilityIds(keyword, t));
  const needle = keyword.trim().toLowerCase();
  if (!needle) return [];

  for (const target of callTargets) {
    if (target && target.toLowerCase().includes(needle)) matched.add(target);
  }
  return [...matched];
}

/**
 * 同一展示名挂多个协议时，返回「展示名 → 协议列表」。
 *
 * 表格拿到这个映射后在名称旁追加协议标签；只有一种协议（或没有协议）的名称不返回，
 * 避免正常行也挂着无信息量的标签。
 */
export function duplicateProtocolsByLabel(
  rows: readonly { name: string | null; protocol: PmCallProtocol | null }[],
  t: TFunction,
): Map<string, PmCallProtocol[]> {
  const grouped = new Map<string, Set<PmCallProtocol>>();

  for (const row of rows) {
    if (!row.name || !row.protocol) continue;
    const label = capabilityLabel(t, row.name);
    const protocols = grouped.get(label) ?? new Set<PmCallProtocol>();
    protocols.add(row.protocol);
    grouped.set(label, protocols);
  }

  const duplicated = new Map<string, PmCallProtocol[]>();
  for (const [label, protocols] of grouped) {
    if (protocols.size > 1) duplicated.set(label, [...protocols]);
  }
  return duplicated;
}
