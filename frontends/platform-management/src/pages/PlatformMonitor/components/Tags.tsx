import { Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { enumLabel } from '../labels';
import type { PmCallStatus, PmFailureReason } from '../types';

/** 未收录的码用中性灰：它既不是成功也不是失败，染色会暗示一个并不存在的语义 */
const NEUTRAL_COLORS: [string, string] = ['#64748b', '#f1f5f9'];

/** 浅底 + 深字的配色。用 antd Tag 承载，只把颜色写进 style，不为它另起一套 scss 类 */
const tagStyle = (color: string, background: string) => ({
  color,
  background,
  border: 'none',
  marginInlineEnd: 0,
  padding: '0 8px',
  borderRadius: 4,
  fontSize: 12,
  lineHeight: '20px',
});

/**
 * 终态五态配色。
 *
 * 只有 `failed` 是红的。取消 / 执行中 / 未知道三态**不进成功率的分母**（契约口径），
 * 若一并染红，表格里红标的条数会明显多于统计卡报的失败数，读起来像对不上账。
 */
const STATUS_COLORS: Record<PmCallStatus, [string, string]> = {
  success: ['#059669', '#ecfdf5'],
  failed: ['#dc2626', '#fef2f2'],
  cancelled: ['#64748b', '#f1f5f9'],
  running: ['#2563eb', '#eff6ff'],
  unknown: ['#64748b', '#f1f5f9'],
};

/** 调用记录 / attempt 表的状态列 */
export function CallStatusTag({ status }: { status: PmCallStatus }) {
  const { t } = useTranslation();
  // 后端新增终态时查不到配色 —— 直接下标会崩在 undefined 解构上，退中性灰并把原码显示出来
  const [color, background] = STATUS_COLORS[status] ?? NEUTRAL_COLORS;
  return (
    <Tag style={tagStyle(color, background)}>{enumLabel(t, 'platformMonitor.callStatus', status)}</Tag>
  );
}

/**
 * 失败原因五档。
 *
 * 五档各一色，色差只用于区分、**不表达严重度排序** —— 「文件格式异常」和「本体抽取失败」
 * 是同一种失败，把它染得更红会暗示一个并不存在的优先级。
 */
const REASON_COLORS: Record<PmFailureReason, [string, string]> = {
  ontology: ['#dc2626', '#fef2f2'],
  compile: ['#db2777', '#fdf2f8'],
  format: ['#d97706', '#fff7ed'],
  parse: ['#ea580c', '#fff7ed'],
  detect: ['#e11d48', '#fff1f2'],
};

export function FailureReasonTag({ reason }: { reason: PmFailureReason }) {
  const { t } = useTranslation();
  const [color, background] = REASON_COLORS[reason] ?? NEUTRAL_COLORS;
  return (
    <Tag style={tagStyle(color, background)}>{enumLabel(t, 'platformMonitor.failureReason', reason)}</Tag>
  );
}
