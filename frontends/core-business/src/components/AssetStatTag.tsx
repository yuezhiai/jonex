import type { CSSProperties, ReactNode } from 'react';
import { Tag } from 'antd';
import {
  ApartmentOutlined,
  FileTextOutlined,
  RobotOutlined,
  ShareAltOutlined,
} from '@ant-design/icons';
import type { DomainKnowledgeDetail, WikiStats } from '@/types/domainKnowledge';
import { isLlmWikiType } from '@/utils/kbType';

// 页头统计标签的标签纸：它们是背景信息，不该和主标题、右侧操作按钮抢注意力。
// 底色统一走冷灰（与卡片边框同族），只有图标带一点低饱和点缀色区分语义——
// antd 预设 color（blue / green / orange）的色块饱和度过高，会把页头切成三段。
const TAG_STYLE: CSSProperties = {
  margin: 0,
  padding: '1px 10px',
  borderRadius: 6,
  background: '#f5f8fb',
  border: '1px solid #e8eef4',
  color: 'var(--yx-text-secondary)',
  fontSize: 12,
  fontWeight: 500,
  // 图标走 children 而非 Tag 的 icon prop 渲染——后者会丢掉图标自己的 style，
  // 低饱和点缀色就没了；间距改由这里的 gap 承担
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
};

// 数值是这些标签的信息主体，比标签文字深一档、重一档
const VALUE_STYLE: CSSProperties = { color: '#33475b', fontWeight: 600 };

// 图标点缀色：同一明度、低饱和的三个色相（雾蓝 / 雾青 / 雾驼）。
// 按标签自左向右的位次取用——同一页头最多三个标签，换一套口径（本体 / Wiki）时色相跟着
// 位次走，不必给每个语义单独定色。
const ICON_COLORS = ['#7d95ad', '#7fa094', '#a8907a'] as const;

const iconStyle = (tone: 0 | 1 | 2): CSSProperties => ({
  color: ICON_COLORS[tone],
  // inline-flex 让包裹层收缩到图标自身高度：否则行盒会带上基线以下的空隙，
  // 标签被撑高，和同排其他元素对不齐
  display: 'inline-flex',
  alignItems: 'center',
});

export interface AssetStatTagItem {
  /** 标签文字，如「文档总数」 */
  label: string;
  /** 统计值，渲染时按当前 locale 加千分位 */
  value: number;
  icon: ReactNode;
  /** 图标点缀色的位次，按标签在页头中自左向右的顺序传；缺省 0 */
  tone?: 0 | 1 | 2;
}

/**
 * 页头统计标签：图标 + 名称 + 数值。
 *
 * 知识库详情页与「知识库设置」页的页头共用这一套外观——两处统计口径不同
 * （本体实例/关系实例 vs Wiki 主题/Wiki 概念），但视觉上必须是同一个东西。
 */
export default function AssetStatTag({ label, value, icon, tone = 0 }: AssetStatTagItem) {
  return (
    <Tag style={TAG_STYLE}>
      <span style={iconStyle(tone)}>{icon}</span>
      {/* 标签名与数值同处一个 flex 子项：数值要贴着标签名，只跟图标之间留 gap */}
      <span>
        {label}
        <span style={VALUE_STYLE}> {value.toLocaleString()}</span>
      </span>
    </Tag>
  );
}

/**
 * 页头三个统计标签：按库型整组二选一，不混搭——llm-wiki 不跑本体抽取，detail 的
 * entityCount / relationCount 是 Wiki 图谱的原始节点/边计数，套「本体实例 / 关系实例」
 * 是误标；反过来，lightrag 的「本体实例」也不该拿 Wiki 的主题/概念口径去顶。
 *
 * 知识库详情页与「知识库设置」页共用本函数，两处页头因此永远同款。
 */
export function buildAssetTags(
  t: (key: string, options?: Record<string, unknown>) => string,
  detail: DomainKnowledgeDetail,
  wikiStats: WikiStats | null,
): AssetStatTagItem[] {
  const documents: AssetStatTagItem = {
    label: t('compile.statDocuments'),
    value: detail.documentCount,
    icon: <FileTextOutlined />,
    tone: 0,
  };
  if (!isLlmWikiType(detail.kbType)) {
    return [
      documents,
      {
        label: t('domainKnowledge.ontologyInstances'),
        value: detail.entityCount,
        icon: <RobotOutlined />,
        tone: 1,
      },
      {
        label: t('domainKnowledge.relationInstances'),
        value: detail.relationCount,
        icon: <ShareAltOutlined />,
        tone: 2,
      },
    ];
  }
  return [
    documents,
    { label: t('compile.wikiEntities'), value: wikiStats?.entities || 0, icon: <ApartmentOutlined />, tone: 1 },
    { label: t('compile.wikiConcepts'), value: wikiStats?.concepts || 0, icon: <RobotOutlined />, tone: 2 },
  ];
}
