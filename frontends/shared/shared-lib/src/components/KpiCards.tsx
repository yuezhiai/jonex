import type { ReactNode } from 'react';
import { Skeleton } from 'antd';

export interface KpiCardItem {
  key: string;
  /** 已翻译的标签 */
  label: ReactNode;
  /** 已格式化的展示值 */
  value: ReactNode;
  /** 标签行右端的角标，如「23 个未完成」 */
  badge?: ReactNode;
  /** 数值后的补充说明，如「近 7 天新增 +12,340」 */
  delta?: ReactNode;
  /** 图标，传了才占左侧图标块 */
  icon?: ReactNode;
  /** 图标色调，用于图标与 8% 底色 */
  iconColor?: string;
  onClick?: () => void;
}

export interface KpiCardsProps {
  items: KpiCardItem[];
  loading?: boolean;
  /** 骨架屏占位卡数，与真实卡数保持一致避免布局跳动 */
  skeletonCount?: number;
}

const SKELETON_LABEL = { width: 72, height: 17 } as const;
const SKELETON_VALUE = { width: 104, height: 29 } as const;

/**
 * KPI 卡组：标签（+ 角标）/ 数值（+ 说明）。
 *
 * 卡片可点时不换成 button —— button 自带一套居中与默认样式，
 * 为一张卡的交互改结构不划算，这里挂 role/tabIndex 并提供键盘触发。
 */
export default function KpiCards({ items, loading, skeletonCount = 4 }: KpiCardsProps) {
  const count = loading ? skeletonCount : items.length;
  const gridClass = `yx-kpi-grid${count === 5 ? ' yx-kpi-grid--five' : ''}`;

  if (loading) {
    return (
      <div className={gridClass}>
        {Array.from({ length: skeletonCount }, (_, i) => (
          <div className="yx-card yx-kpi" key={i}>
            <div className="yx-kpi-main">
              <div className="yx-kpi-top">
                <Skeleton.Input active size="small" style={SKELETON_LABEL} />
              </div>
              <div className="yx-kpi-value-row">
                <Skeleton.Input active size="small" style={SKELETON_VALUE} />
              </div>
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className={gridClass}>
      {items.map((item) => {
        const clickable = item.onClick !== undefined;
        return (
          <div
            className={`yx-card yx-kpi${clickable ? ' yx-kpi--clickable' : ''}`}
            key={item.key}
            role={clickable ? 'button' : undefined}
            tabIndex={clickable ? 0 : undefined}
            onClick={item.onClick}
            onKeyDown={
              clickable
                ? (event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    // 空格默认会滚动页面
                    event.preventDefault();
                    item.onClick?.();
                  }
                : undefined
            }
          >
            {item.icon && (
              <span
                className="yx-kpi-icon"
                style={{ color: item.iconColor, background: item.iconColor ? `${item.iconColor}14` : undefined }}
              >
                {item.icon}
              </span>
            )}
            <div className="yx-kpi-main">
              <div className="yx-kpi-top">
                <span className="yx-kpi-label">{item.label}</span>
                {item.badge && <span className="yx-kpi-badge">{item.badge}</span>}
              </div>
              <div className="yx-kpi-value-row">
                <span className="yx-kpi-value">{item.value}</span>
                {item.delta && <span className="yx-kpi-delta">{item.delta}</span>}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
