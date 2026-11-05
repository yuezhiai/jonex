import React from 'react';
import {
  ShareAltOutlined,
  LikeOutlined,
  DislikeOutlined,
  RobotOutlined,
  ApartmentOutlined,
  ReadOutlined,
  BarsOutlined,
  TableOutlined,
} from '@ant-design/icons';
import type { OntologyStatistics, WikiStats } from '@/types/domainKnowledge';

// ── Types ──────────────────────────────────────────────────
export interface TabConfig {
  key: string;
  label: string;
  count: number;
  icon: React.ReactNode;
}

// WikiStats 已迁到 @/types/domainKnowledge：两个页头（详情页 / 设置页）与面板都要用，
// 类型不该住在页面模块里。消费者请直接从 types 引。

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
    { key: 'columnBrowse', label: t('domainKnowledge.columnBrowse'), count: 0, icon: <BarsOutlined /> },
    // { key: 'like', label: '用户赞采纳', count: likeCount, icon: <LikeOutlined /> },
    // { key: 'dislike', label: '用户踩采纳', count: dislikeCount, icon: <DislikeOutlined /> },
  ];
}

// ── Stats 卡片 ─────────────────────────────────────────────
// 统计卡整体已移除（构建函数一并删掉）：两类库的三个总数现在都由宿主页面（知识库详情页）
// 页头用标签展示——lightrag 取 KB 详情，llm-wiki 的 Wiki 主题/概念取宿主页下发的 wikiStats。
// 面板里再留一份就是同一组数字在页面上出现两次。

// ── [jonex] openkb（LLM-Wiki）分支：Wiki 页面树 + Wiki 图谱 ──

export function buildOpenkbTabs(
  t: (key: string, options?: Record<string, unknown>) => string,
  wiki: WikiStats,
  /** [jonex] jonex_wiki 走新阅读界面（目录 → 文章 → References），文案用「Wiki 界面」 */
  isJonexWiki = false,
): TabConfig[] {
  // [jonex] 语义化 key：'wikiPages'/'wikiGraph'/'wikiColumns'（与 lightrag 的
  // ontology/relation/graph 区分）。页面在 kbType 识别后由调用方把 activeTab
  // 设到有效 key（见 CompileAssetsPanel 回退）。
  const tabs: TabConfig[] = [
    {
      key: 'wikiPages',
      label: isJonexWiki ? t('compile.wikiReader') : t('compile.wikiPages'),
      count: wiki.summaries + wiki.concepts + wiki.entities,
      icon: <ReadOutlined />,
    },
  ];
  // [jonex] jonex_wiki 不再有「主题地图与关联」tab：主题地图（章节目录树）与阅读
  // 界面左侧目录栏信息完全重复，1-hop 关联也已由条目页的关联/反向链接承担。
  // openkb 保持原「页面树 + 实体关系图」两 tab，不受影响。
  if (isJonexWiki) {
    // 版本化知识图谱：节点类型 → 节点 → 关联，使用统一图查询服务和对外接口。
    // 仍复用本体链路的列式视觉基础件；openkb 保持原图谱视图。
    tabs.push({
      key: 'wikiColumns',
      label: t('compile.wikiColumns'),
      count: 0,
      icon: <TableOutlined />,
    });
  } else {
    tabs.push({
      key: 'wikiGraph',
      label: t('compile.wikiGraph'),
      count: 0,
      icon: <ApartmentOutlined />,
    });
  }
  return tabs;
}
