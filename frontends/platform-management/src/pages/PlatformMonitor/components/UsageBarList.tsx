import { Empty, Flex, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import { fmtCompact } from '../format';
import type { PmModelShareItem } from '../types';

/** 用同一色系的深浅区分条目，不引入第二组语义色（这一块讲的是「谁占得多」，不是好坏） */
const PALETTE = ['#1248a0', '#2563eb', '#3b82f6', '#60a5fa', '#93c5fd'];

interface Props {
  items: PmModelShareItem[];
  loading?: boolean;
}

/**
 * 横向占比条列表（模型使用分布）。
 *
 * 百分比由 `count` 与同组总量算出，**不取数据里的写死值** —— 否则改一个 count
 * 就要同步改百分比，两处迟早对不上。设计稿里百分比是整数，这里也按整数渲染。
 *
 * 三态（骨架 / 空态 / 列表）共用 `.yx-pm-bar-slot` 一块高度下限，理由见 index.scss。
 */
export default function UsageBarList({ items, loading }: Props) {
  const { t } = useTranslation();
  const isEmpty = !items.length;
  const total = items.reduce((sum, item) => sum + item.count, 0) || 1;

  return (
    <div className="yx-pm-bar-slot">
      {/* 骨架只在「还没拿到过任何数据」时上：换时间窗重取期间保留上一帧，
          否则这张卡会「列表 → 骨架 → 列表」闪一下（同 TrendAreaChart） */}
      {loading && isEmpty ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : isEmpty ? (
        <Flex align="center" justify="center" style={{ flex: 1 }}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.empty.noData')} />
        </Flex>
      ) : (
        <ul className="yx-pm-bar-list">
          {items.map((item, index) => {
            const share = (item.count / total) * 100;
            return (
              <li key={item.key} className="yx-pm-bar-item">
                <div className="yx-pm-bar-head">
                  <span className="yx-pm-bar-label" title={item.label}>
                    {item.label}
                  </span>
                  <span className="yx-pm-bar-value">
                    {fmtCompact(item.count)}
                    <em className="yx-pm-bar-percent">{Math.round(share)}%</em>
                  </span>
                </div>
                <div className="yx-pm-bar-track">
                  <span
                    className="yx-pm-bar-fill"
                    style={{ width: `${Math.min(share, 100)}%`, background: PALETTE[index % PALETTE.length] }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
