import i18n from 'i18next';
import type {
  CompiledRelationCardinality,
  OntologyAttrType,
  OntologyRelationType,
  CompileScope,
  CompileTrigger,
} from '@/types/domainKnowledge';

export const ATTR_TYPE_OPTIONS: { label: string; value: OntologyAttrType }[] = [
  { label: i18n.t('compile.attrType.string'), value: '字符串' },
  { label: i18n.t('compile.attrType.text'), value: '文本' },
  { label: i18n.t('compile.attrType.number'), value: '数值' },
  { label: i18n.t('compile.attrType.date'), value: '日期' },
  { label: i18n.t('compile.attrType.enum'), value: '枚举' },
  { label: i18n.t('compile.attrType.boolean'), value: '布尔' },
];

/** OntologyAttrType → i18n key（labelKey 供 t() 渲染 Select option label 用） */
export const ATTR_TYPE_LABEL_KEYS: Record<string, string> = {
  字符串: 'compile.attrType.string',
  文本: 'compile.attrType.text',
  数值: 'compile.attrType.number',
  日期: 'compile.attrType.date',
  枚举: 'compile.attrType.enum',
  布尔: 'compile.attrType.boolean',
};

export const RELATION_CARDINALITY_OPTIONS: { label: string; value: CompiledRelationCardinality }[] = [
  { label: i18n.t('compile.cardinality.oneToOne'), value: 'one_to_one' },
  { label: i18n.t('compile.cardinality.oneToMany'), value: 'one_to_many' },
  { label: i18n.t('compile.cardinality.manyToOne'), value: 'many_to_one' },
  { label: i18n.t('compile.cardinality.manyToMany'), value: 'many_to_many' },
  { label: i18n.t('compile.cardinality.custom'), value: 'custom' },
];

export const RELATION_CARDINALITY_TEXT: Record<CompiledRelationCardinality, string> = {
  one_to_one: i18n.t('compile.cardinality.oneToOne'),
  one_to_many: i18n.t('compile.cardinality.oneToMany'),
  many_to_one: i18n.t('compile.cardinality.manyToOne'),
  many_to_many: i18n.t('compile.cardinality.manyToMany'),
  custom: i18n.t('compile.cardinality.custom'),
};

/** CompiledRelationCardinality → i18n key */
export const RELATION_CARDINALITY_LABEL_KEYS: Record<CompiledRelationCardinality, string> = {
  one_to_one: 'compile.cardinality.oneToOne',
  one_to_many: 'compile.cardinality.oneToMany',
  many_to_one: 'compile.cardinality.manyToOne',
  many_to_many: 'compile.cardinality.manyToMany',
  custom: 'compile.cardinality.custom',
};

export const RELATION_TYPE_TAG: Record<CompiledRelationCardinality, { bg: string; color: string }> = {
  one_to_one: { bg: '#ecfdf5', color: '#059669' },
  one_to_many: { bg: '#eff6ff', color: '#2563eb' },
  many_to_one: { bg: '#eef2ff', color: '#4f46e5' },
  many_to_many: { bg: '#fef3c7', color: '#d97706' },
  custom: { bg: '#f3e8ff', color: '#7c3aed' },
};

export const ENGINE_MODEL_OPTIONS: { label: string; value: string }[] = [
  { label: 'Claude Sonnet 4.6', value: 'claude-sonnet-4-6' },
  { label: 'Claude Opus 4.8', value: 'claude-opus-4-8' },
  { label: 'Claude Haiku 4.5', value: 'claude-haiku-4-5-20251001' },
  { label: 'GLM-4-9B', value: 'glm4:9b' },
  { label: 'GPT-4o', value: 'gpt-4o' },
  { label: 'DeepSeek-V3', value: 'deepseek-v3' },
];

export const DEFAULT_ENGINE_MODEL = 'claude-sonnet-4-6';

export const COMPILE_SCOPE_OPTIONS: { label: string; value: CompileScope }[] = [
  { label: i18n.t('compile.scope.single'), value: 'single' },
  { label: i18n.t('compile.scope.whole'), value: 'whole' },
];

export const COMPILE_TRIGGER_OPTIONS: { label: string; value: CompileTrigger }[] = [
  { label: i18n.t('compile.trigger.upload'), value: 'upload' },
  { label: i18n.t('compile.trigger.update'), value: 'update' },
];

export const SKILL_OPTIONS: { label: string; value: string }[] = [
  { label: i18n.t('compile.skill.layoutAnalysis'), value: '文档版面分析' },
  { label: i18n.t('compile.skill.multimodalFusion'), value: '多模态融合检索' },
  { label: i18n.t('compile.skill.semanticSimilarity'), value: '语义相似度计算' },
  { label: i18n.t('compile.skill.entityLinking'), value: '实体链接' },
  { label: i18n.t('compile.skill.relationExtraction'), value: '关系抽取' },
];

export function normalizeRelationType(raw?: string): OntologyRelationType {
  const mapping: Record<string, OntologyRelationType> = {
    one_to_one: '一对一',
    one_to_many: '一对多',
    many_to_one: '多对一',
    many_to_many: '多对多',
    custom: '自定义',
    '1:1': '一对一',
    '1:N': '一对多',
    'M:1': '多对一',
    'M:N': '多对多',
  };
  return mapping[(raw || '').toLowerCase()] || '自定义';
}

export function normalizeAttrType(raw?: string): OntologyAttrType {
  const mapping: Record<string, OntologyAttrType> = {
    string: '字符串',
    text: '文本',
    number: '数值',
    date: '日期',
    enum: '枚举',
    boolean: '布尔',
    字符串: '字符串',
    文本: '文本',
    数值: '数值',
    日期: '日期',
    枚举: '枚举',
    布尔: '布尔',
  };
  return mapping[raw || ''] || '字符串';
}

export function normalizeRelationCardinality(raw?: string): CompiledRelationCardinality {
  const allowed: CompiledRelationCardinality[] = [
    'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many', 'custom',
  ];
  return allowed.includes((raw || '') as CompiledRelationCardinality) ? (raw as CompiledRelationCardinality) : 'custom';
}
