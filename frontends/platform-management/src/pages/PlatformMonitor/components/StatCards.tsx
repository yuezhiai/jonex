import { KpiCards } from '@jonex/shared-lib';
import type { PmKpiCard } from '../types';

interface Props {
  items: PmKpiCard[];
  loading?: boolean;
  /** 骨架屏占位卡数，与真实卡数保持一致避免布局跳动 */
  skeletonCount?: number;
}

/**
 * 平台监控的 KPI 卡组：把视图模型转成通用 KpiCardItem。
 * 卡片本身的样式与骨架屏在 shared-lib 的 KpiCards 里，模块内不再单独一套。
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
        badge: item.badge,
        onClick: item.onClick,
      }))}
    />
  );
}
