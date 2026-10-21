import { Segmented } from 'antd';

interface Props<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  /** 默认 small —— 卡片右上角的筛选器不适合用默认尺寸 */
  size?: 'small' | 'middle' | 'large';
}

/** 卡片右上角分段器的统一受控封装 */
export default function SegmentedFilter<T extends string>({ value, onChange, options, size = 'small' }: Props<T>) {
  return <Segmented size={size} value={value} onChange={(v) => onChange(v as T)} options={options} />;
}
