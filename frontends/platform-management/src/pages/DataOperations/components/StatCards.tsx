import type { ReactNode } from 'react';
import { KpiCards } from '@jonex/shared-lib';
import {
  ApartmentOutlined,
  AppstoreOutlined,
  BookOutlined,
  BulbOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  LineChartOutlined,
  LinkOutlined,
  NodeIndexOutlined,
  PercentageOutlined,
  SearchOutlined,
  TeamOutlined,
} from '@ant-design/icons';

export interface StatCardItem {
  key: string;
  /** 已翻译的标签 */
  label: string;
  /** 已格式化的展示值（千分位由数据层保证） */
  value: string;
  /** 值下方的小徽标，如「近 7 天新增 +12,340」 */
  delta?: ReactNode;
  /** 图标底色 */
  accent: string;
  /** 图标标识，映射见下方 ICONS */
  iconKey: string;
}

const ICONS: Record<string, ReactNode> = {
  database: <DatabaseOutlined />,
  source: <CloudServerOutlined />,
  link: <LinkOutlined />,
  percent: <PercentageOutlined />,
  object: <AppstoreOutlined />,
  relation: <ApartmentOutlined />,
  entity: <NodeIndexOutlined />,
  concept: <BulbOutlined />,
  search: <SearchOutlined />,
  kb: <BookOutlined />,
  user: <TeamOutlined />,
  avg: <LineChartOutlined />,
};

interface Props {
  items: StatCardItem[];
  loading?: boolean;
  /** 骨架屏占位卡数，与真实卡数保持一致避免布局跳动 */
  skeletonCount?: number;
}

/**
 * 数据运营的 KPI 卡组：带图标的一版。
 * 卡片样式与骨架屏统一在 shared-lib 的 KpiCards，这里只负责图标映射。
 */
export default function StatCards({ items, loading, skeletonCount = 4 }: Props) {
  return (
    <KpiCards
      loading={loading}
      skeletonCount={skeletonCount}
      items={items.map((item) => ({
        key: item.key,
        label: item.label,
        value: item.value,
        delta: item.delta,
        icon: ICONS[item.iconKey],
        iconColor: item.accent,
      }))}
    />
  );
}
