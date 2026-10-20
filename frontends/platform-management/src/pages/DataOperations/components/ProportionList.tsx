import { Empty, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoProportionItem } from '../types';

/**
 * 默认色板（按 `items` 下标取）。末位中性灰与 `AssetTypeTag` 的 `--other` 同色 ——
 * 色板少一个颜色时下标会取模回绕，第 7 行会拿到第 1 行的蓝，看上去像「其他」属于文档。
 *
 * ⚠️ 这份色板由几张占比图共用，**不要为了某一页调换顺序**：资产构成图只展示五类，
 * 需要自己的配色时在条目上写 `color`（见 `DoProportionItem.color`）。
 */
const PALETTE = ['#3b82f6', '#10b981', '#8b5cf6', '#0ea5e9', '#f97316', '#ec4899', '#6b7280'];

interface Props {
  items: DoProportionItem[];
  loading?: boolean;
  /** 数值列展示的内容，默认显示 count 的千分位 */
  renderValue?: (item: DoProportionItem, percent: number) => React.ReactNode;
  /**
   * 覆盖「百分比」口径：返回值同时用于显示与条宽。
   * 不传则用 count 占同组总量的比例。检索方式的「参与率」走这里。
   */
  percentOf?: (item: DoProportionItem, share: number) => number;
  colorOf?: (item: DoProportionItem, index: number) => string;
  /**
   * 覆盖百分比**分母**（不传则取 `items` 的 count 之和）。
   *
   * 给「只展示一部分、但口径仍是全部」的图用：资产构成图只列五类，隐藏的三类
   * 必须仍留在分母里，否则百分比会被重新归一（「文档」会从 42.9% 涨到 66.7%）。
   */
  total?: number;
}

/**
 * 横向占比条列表。
 * 不用 antd Progress：它自带的内外边距与截图里的紧凑列表不符。
 * 百分比一律由 count 与同组总量算出，不取常量里的写死值，保证与数字自洽。
 */
export default function ProportionList({ items, loading, renderValue, percentOf, colorOf, total: totalProp }: Props) {
  const { t } = useTranslation();

  if (loading) return <Skeleton active paragraph={{ rows: 5 }} />;
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('dataOperations.empty.noData')} />;

  const total = totalProp ?? (items.reduce((sum, item) => sum + item.count, 0) || 1);

  return (
    <ul className="yx-do-proportion">
      {items.map((item, index) => {
        const share = (item.count / total) * 100;
        const percent = percentOf ? percentOf(item, share) : share;
        const color = item.color ?? (colorOf ? colorOf(item, index) : PALETTE[index % PALETTE.length]);

        return (
          <li key={item.key} className="yx-do-proportion-item">
            <div className="yx-do-proportion-head">
              <span className="yx-do-proportion-label">{item.labelKey ? t(item.labelKey) : item.label}</span>
              <span className="yx-do-proportion-value">
                {renderValue ? renderValue(item, percent) : item.count.toLocaleString()}
                <em className="yx-do-proportion-percent">{percent.toFixed(1)}%</em>
              </span>
            </div>
            <div className="yx-do-proportion-track">
              <span
                className="yx-do-proportion-bar"
                style={{ width: `${Math.min(percent, 100)}%`, background: color }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
