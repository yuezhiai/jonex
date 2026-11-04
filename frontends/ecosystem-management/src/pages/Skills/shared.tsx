// 技能页共享常量与工具：Skills 列表页 / 技能详情弹窗双份复用的类别图标、i18n key 与 JSON Schema 处理。
import React from 'react';
import {
  FileImageOutlined,
  AudioOutlined,
  FileTextOutlined,
  VideoCameraOutlined,
  DatabaseOutlined,
  SearchOutlined as FusionSearchOutlined,
} from '@ant-design/icons';
import type { SkillItem } from '../../api/skills';

/** 技能类别 → 类别图标 */
export const CATEGORY_ICONS: Record<string, React.ReactNode> = {
  image: <FileImageOutlined />,
  voice: <AudioOutlined />,
  document: <FileTextOutlined />,
  video: <VideoCameraOutlined />,
  fusion: <FusionSearchOutlined />,
  custom: <DatabaseOutlined />,
};

/** 技能类别 → i18n key（skills.categoryXxx） */
export const CATEGORY_LABEL_KEYS: Record<string, string> = {
  image: 'skills.categoryImage',
  voice: 'skills.categoryVoice',
  document: 'skills.categoryDocument',
  video: 'skills.categoryVideo',
  fusion: 'skills.categoryFusion',
  custom: 'skills.categoryCustom',
};

/** 内置技能 id → 内置技能 i18n key 后缀 */
export const BUILT_IN_SKILL_KEYS: Record<string, string> = {
  skill_image_recognition: 'imageRecognition',
  skill_speech_to_text: 'speechToText',
  skill_document_layout: 'documentLayout',
  skill_video_understanding: 'videoUnderstanding',
  skill_multimodal_search: 'multimodalSearch',
  skill_data_extraction: 'dataExtraction',
};

/** 内置技能 id → 默认标签（标签由 i18n 翻译） */
const BUILT_IN_SKILL_TAGS: Record<string, string[]> = {
  imageRecognition: ['image', 'ocr', 'recognition'],
  speechToText: ['speech', 'transcription', 'asr'],
  documentLayout: ['document', 'layout', 'structured'],
  videoUnderstanding: ['video', 'frameSampling', 'analysis'],
  multimodalSearch: ['search', 'fusion', 'semantic'],
  dataExtraction: ['extraction', 'structured', 'document'],
};

/** 类别 → 浅底 + 同色图标（比高饱和渐变安静，保留分类辨识度） */
export const CATEGORY_TINTS: Record<string, { background: string; color: string }> = {
  image: { background: '#e8f1ff', color: '#1d6ff2' },
  voice: { background: '#e6f7ef', color: '#0f9d63' },
  document: { background: '#fff2e3', color: '#d97706' },
  video: { background: '#f1ecff', color: '#7c4dff' },
  fusion: { background: '#fef0f0', color: '#dc2626' },
  custom: { background: '#e6f7f8', color: '#0891b2' },
};

/** 列表卡片与详情弹窗共用的文案解析：内置技能走 i18n，自定义工具用接口原文 */
export function getSkillCopy(skill: SkillItem, t: (key: string) => string) {
  const key = BUILT_IN_SKILL_KEYS[skill.id];
  if (!key) {
    return {
      name: skill.name,
      description: skill.description || '',
      instruction: skill.instruction,
      tags: skill.tags,
    };
  }
  return {
    name: t(`skills.builtIn.${key}.name`),
    description: t(`skills.builtIn.${key}.description`),
    instruction: t(`skills.builtIn.${key}.instruction`),
    tags: (BUILT_IN_SKILL_TAGS[key] || []).map((tag) => t(`skills.tags.${tag}`)),
  };
}

/** JSON Schema 字段名 → 描述 i18n key（localizeSchemaDescriptions 按父键查找） */
const SCHEMA_DESCRIPTION_KEYS: Record<string, string> = {
  file_url: 'skills.schemaDescriptions.fileUrl',
  tasks: 'skills.schemaDescriptions.tasks',
  language: 'skills.schemaDescriptions.language',
  speaker_diarization: 'skills.schemaDescriptions.speakerDiarization',
  output_format: 'skills.schemaDescriptions.outputFormat',
  sample_rate: 'skills.schemaDescriptions.sampleRate',
  modalities: 'skills.schemaDescriptions.modalities',
  top_k: 'skills.schemaDescriptions.topK',
  schema: 'skills.schemaDescriptions.schema',
  doc_type: 'skills.schemaDescriptions.documentType',
};

/** 将 schema 中父键命中的 description 字段翻译为本地化文案（按父字段名查表） */
export function localizeSchemaDescriptions(value: unknown, t: (key: string) => string, parentKey = ''): unknown {
  if (Array.isArray(value)) return value.map((item) => localizeSchemaDescriptions(item, t, parentKey));
  if (!value || typeof value !== 'object') return value;

  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => {
      if (key === 'description' && typeof item === 'string') {
        const translationKey = SCHEMA_DESCRIPTION_KEYS[parentKey];
        return [key, translationKey ? t(translationKey) : item];
      }
      return [key, localizeSchemaDescriptions(item, t, key)];
    }),
  );
}

/** JSON Schema 美化展示：JSON.stringify(schema, null, 2)，失败回退 {} */
export function formatJsonSchema(schema: Record<string, unknown>): string {
  try {
    return JSON.stringify(schema, null, 2);
  } catch {
    return '{}';
  }
}
