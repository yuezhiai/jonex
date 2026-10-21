import { Alert, Button } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  classifyMonitorError,
  monitorErrorTextKeys,
  readErrorMessage,
  readRangeErrorDetails,
} from '../errorText';
import { PM_TIME_PATTERN, fmtPmTime } from '../time';

interface Props {
  error: unknown;
  /** 不传 = 不给重试按钮（如抽屉里的一次性下拉，重试入口在别处） */
  onRetry?: () => void;
}

/**
 * 分区级错误提示。
 *
 * 监控页的四块内容（KPI / 趋势 / 列表 / 分布）各自取数，一块挂了不该把整页打成错误页 ——
 * 那会让另外三块已拿到的数据也一起消失。所以错误落在**它自己的分区里**：这张卡的位置显示
 * 一行带重试的提示，其余分区照常可用。
 */
export default function ModuleError({ error, onRetry }: Props) {
  const { t } = useTranslation();
  const kind = classifyMonitorError(error);
  const keys = monitorErrorTextKeys(kind);
  // 4004 带结构化 details：把实际上限 / 保留期与可查区间渲染进文案，而不是写死 30 天
  const details = readRangeErrorDetails(error);
  const params =
    kind === 'rangeTooLong'
      ? { maxDays: details.maxDays ?? 30 }
      : kind === 'outOfRetention'
        ? {
            days: details.retentionDays ?? '-',
            from: fmtPmTime(details.queryableRange?.from, PM_TIME_PATTERN.full) ?? '-',
            to: fmtPmTime(details.queryableRange?.to, PM_TIME_PATTERN.full) ?? '-',
          }
        : undefined;
  // 已知错误码用本地化文案（后端 message 会随 X-Lang 变，但不解释「你该做什么」）；
  // 未知错误只能展示后端 / 网络层给的原话，否则用户只知道「失败了」却不知道原因
  const description = keys.description ? t(keys.description, params) : readErrorMessage(error) || undefined;

  return (
    <Alert
      type="error"
      showIcon
      message={t(keys.title)}
      description={description}
      action={
        onRetry ? (
          <Button size="small" onClick={onRetry}>
            {t('platformMonitor.error.retry')}
          </Button>
        ) : undefined
      }
    />
  );
}
