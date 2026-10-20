import { DatePicker, message } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import { useTranslation } from 'react-i18next';
import { DO_WINDOW_PRESET_DAYS } from '../../../constants/dataOperations';
import type { DoWindow } from '../types';

const { RangePicker } = DatePicker;

/** 后端 `_parse_trend_range` 的上限，超了会被判 1001 —— 在这里就拦住，别让用户点出一个必然失败的请求 */
const MAX_SPAN_DAYS = 366;

type RangeValue = [Dayjs, Dayjs];

/**
 * `DoWindow` → RangePicker 值。
 *
 * 相对窗口（`{ days }`）反解成「截至今天的 N 天」：后端 `days=7` 与前端预设都按含今天算，
 * 所以起点是 `今天 - (N-1)`，这样预设选中后显示出来的区间与后端统计的区间是同一段。
 */
function toRange(value: DoWindow): RangeValue {
  const today = dayjs();
  if ('startDate' in value) return [dayjs(value.startDate), dayjs(value.endDate)];
  return [today.subtract(value.days - 1, 'day'), today];
}

/**
 * RangePicker 值 → `DoWindow`。
 *
 * 起止正好是「截至今天的 N 天」且 N 命中预设时，**回落成 `{ days }`** 而不是 `{ startDate, endDate }`：
 * 两种形态在后端等价，但 `days` 是相对窗口，用户在另一天打开页面时统计区间会跟着走，
 * 与预设按钮的口径一致（否则「手选了一个看起来像近 7 天的区间」会锁死在那两个日期上）。
 */
function toWindow(range: RangeValue): DoWindow {
  const [start, end] = range;
  const days = end.diff(start, 'day') + 1;
  if (end.isSame(dayjs(), 'day') && DO_WINDOW_PRESET_DAYS.includes(days)) return { days };
  return { startDate: start.format('YYYY-MM-DD'), endDate: end.format('YYYY-MM-DD') };
}

interface Props {
  value: DoWindow;
  onChange: (value: DoWindow) => void;
}

/**
 * 趋势卡的统计区间选择器。
 *
 * 一个控件同时承担「预设」与「自定义」：预设走 RangePicker 自带的 presets 面板（点开下拉即可选），
 * 自定义直接在同一个输入框里选日期 —— 比「Segmented 切到自定义再冒出一个 RangePicker」少一次交互，
 * 也少一个需要维护的中间状态。
 *
 * 选中的区间驱动**本页的趋势图、喂抽屉的列表与四个抽屉**（KPI 卡与构成图固定近 7 天，不受它驱动），
 * 所以它必须与列表、抽屉共用一个 state —— 见各页的 `window`。
 */
export default function WindowRangePicker({ value, onChange }: Props) {
  const { t } = useTranslation();

  const handleChange = (range: RangeValue | null) => {
    if (!range || !range[0] || !range[1]) return;
    const days = range[1].diff(range[0], 'day') + 1;
    if (days > MAX_SPAN_DAYS) {
      message.warning(t('dataOperations.window.tooLong', { max: MAX_SPAN_DAYS }));
      return;
    }
    onChange(toWindow(range));
  };

  return (
    <RangePicker
      size="small"
      // 与 OperationLog 的时间筛选器同宽；卡片右上角不适合放任它按内容撑开
      style={{ width: 240 }}
      allowClear={false}
      format="YYYY-MM-DD"
      value={toRange(value)}
      onChange={(range) => handleChange(range as RangeValue | null)}
      // 统计口径是「截至今天」，选未来日期没有意义
      disabledDate={(current) => current.isAfter(dayjs(), 'day')}
      presets={DO_WINDOW_PRESET_DAYS.map((days) => ({
        label: t('dataOperations.window.preset', { days }),
        value: [dayjs().subtract(days - 1, 'day'), dayjs()] as RangeValue,
      }))}
      placeholder={[t('dataOperations.window.start'), t('dataOperations.window.end')]}
    />
  );
}
