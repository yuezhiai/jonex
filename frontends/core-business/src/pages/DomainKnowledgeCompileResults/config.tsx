import React from 'react';
import {
  FileTextOutlined,
  ShareAltOutlined,
  LikeOutlined,
  DislikeOutlined,
  RobotOutlined,
  ApartmentOutlined,
  ReadOutlined,
} from '@ant-design/icons';
import type { OntologyStatistics } from '@/types/domainKnowledge';

// ── Types ──────────────────────────────────────────────────
export interface TabConfig {
  key: string;
  label: string;
  count: number;
  icon: React.ReactNode;
}

/** [jonex] openkb Wiki 内容统计（getWikiContents(kbId, "") 全库返回的三类页数）。 */
export interface WikiStats {
  summaries: number;
  concepts: number;
  entities: number;
}

// ── Tabs（count 由调用方传入，其中 like/dislike 来自搜索反馈统计） ────────
export function buildTabs(
  t: (key: string, options?: Record<string, unknown>) => string,
  stats: OntologyStatistics,
  likeCount = 0,
  dislikeCount = 0,
): TabConfig[] {
  return [
    {
      key: 'ontology',
      label: t('domainKnowledge.ontologyInstances'),
      count: stats.ontology_instance_count,
      icon: <RobotOutlined />,
    },
    {
      key: 'relation',
      label: t('domainKnowledge.relationInstances'),
      count: stats.ontology_relation_count,
      icon: <ShareAltOutlined />,
    },
    { key: 'graph', label: t('domainKnowledge.graphBreadcrumb'), count: 0, icon: <ApartmentOutlined /> },
    // { key: 'like', label: '用户赞采纳', count: likeCount, icon: <LikeOutlined /> },
    // { key: 'dislike', label: '用户踩采纳', count: dislikeCount, icon: <DislikeOutlined /> },
  ];
}

// ── Stats 卡片 ─────────────────────────────────────────────
export function buildStats(
  t: (key: string, options?: Record<string, unknown>) => string,
  stats: OntologyStatistics,
  ontologyInstanceCount?: number,
  relationInstanceCount?: number,
) {
  return [
    {
      label: t('compile.statDocuments'),
      value: stats.source_file_count,
      icon: <FileTextOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      label: t('domainKnowledge.ontologyInstances'),
      value: ontologyInstanceCount ?? stats.ontology_instance_count,
      icon: <RobotOutlined style={{ color: '#22c55e' }} />,
    },
    {
      label: t('domainKnowledge.relationInstances'),
      value: relationInstanceCount ?? stats.ontology_relation_count,
      icon: <ShareAltOutlined style={{ color: '#f59e0b' }} />,
    },
  ];
}

// ── [jonex] openkb（LLM-Wiki）分支：Wiki 页面树 + Wiki 图谱 ──

export function buildOpenkbTabs(
  t: (key: string, options?: Record<string, unknown>) => string,
  wiki: WikiStats,
): TabConfig[] {
  // [jonex] 语义化 key：'wikiPages'/'wikiGraph'（与 lightrag 的 ontology/relation/graph
  // 区分）。页面在 kbType 识别后由调用方把 activeTab 设到有效 key（见 index.tsx 回退）。
  return [
    {
      key: 'wikiPages',
      label: t('compile.wikiPages'),
      count: wiki.summaries + wiki.concepts + wiki.entities,
      icon: <ReadOutlined />,
    },
    { key: 'wikiGraph', label: t('compile.wikiGraph'), count: 0, icon: <ApartmentOutlined /> },
  ];
}

export function buildOpenkbStats(
  t: (key: string, options?: Record<string, unknown>) => string,
  sourceFileCount: number,
  wiki: WikiStats,
) {
  return [
    {
      label: t('compile.statDocuments'),
      value: sourceFileCount,
      icon: <FileTextOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      label: t('compile.wikiSummaries'),
      value: wiki.summaries,
      icon: <ReadOutlined style={{ color: '#10b981' }} />,
    },
    {
      label: t('compile.wikiConcepts'),
      value: wiki.concepts,
      icon: <RobotOutlined style={{ color: '#06b6d4' }} />,
    },
    {
      label: t('compile.wikiEntities'),
      value: wiki.entities,
      icon: <ApartmentOutlined style={{ color: '#8b5cf6' }} />,
    },
  ];
}
