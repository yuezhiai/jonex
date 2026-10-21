import { Skeleton } from 'antd';
import type { PmKpiCard } from '../types';

interface Props {
  items: PmKpiCard[];
  loading?: boolean;
  /** 骨架屏占位卡数，与真实卡数保持一致避免布局跳动 */
  skeletonCount?: number;
}

/**
 * KPI 卡组。两行结构：标签（+ 右上角角标）/ 数值。
 *
 * 契约里没有环比数据，也不展示口径小字，所以不放趋势箭头与注解行 ——
 * 只有箭头没有百分比的卡会读成「这个数在涨」。
 *
 * 骨架按真实卡的两行字号定高（标签 17px / 数值 33px），而不是用 Skeleton 的段落行 ——
 * 段落行自带行距，比真实内容高出一截，曾经靠卡片 min-height 硬压，结果卡片被撑得空荡荡。
 */
export default function StatCards({ items, loading, skeletonCount = 4 }: Props) {
  // 五张卡（Token 侧多了用量完整率）时网格换 5 轨：4 轨会让第 5 张单独落在下一行
  const count = loading ? skeletonCount : items.length;
  const gridClass = `yx-pm-kpi-grid${count === 5 ? ' yx-pm-kpi-grid--five' : ''}`;

  if (loading) {
    return (
      <div className={gridClass}>
        {Array.from({ length: skeletonCount }, (_, i) => (
          <div className="yx-card yx-pm-kpi" key={i}>
            <div className="yx-pm-kpi-top">
              <Skeleton.Input active size="small" style={{ width: 72, height: 17 }} />
            </div>
            <div className="yx-pm-kpi-value-row">
              <Skeleton.Input active size="small" style={{ width: 104, height: 33 }} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className={gridClass}>
      {items.map((item) => {
        // 可点的卡（如失败数）挂 role/tabIndex 而不是换成 button：button 会带来一整套
        // 默认样式与居中规则，为了一张卡的交互把卡片结构改掉不划算
        const clickable = item.onClick !== undefined;
        return (
          <div
            className={`yx-card yx-pm-kpi${clickable ? ' yx-pm-kpi--clickable' : ''}`}
            key={item.key}
            role={clickable ? 'button' : undefined}
            tabIndex={clickable ? 0 : undefined}
            onClick={item.onClick}
            onKeyDown={
              clickable
                ? (event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    // 空格默认会滚动页面，键盘用户按下去会看到页面跳一下
                    event.preventDefault();
                    item.onClick?.();
                  }
                : undefined
            }
          >
            <div className="yx-pm-kpi-top">
              <span className="yx-pm-kpi-label">{item.label}</span>
              {item.badge && <span className="yx-pm-kpi-badge">{item.badge}</span>}
            </div>
            <div className="yx-pm-kpi-value-row">
              <span className="yx-pm-kpi-value">{item.value}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
