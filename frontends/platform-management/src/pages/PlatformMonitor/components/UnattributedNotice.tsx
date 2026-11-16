import { Alert } from 'antd';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { getUnattributedSummary } from '../../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import { PM_TIME_PATTERN, fmtPmTime } from '../time';
import { fmtCompact, fmtCount, fmtPercent } from '../format';
import type { PmPaneProps } from '../types';

/**
 * 「另有未归因调用」提示。
 *
 * 有些调用没有携带租户标识（`tenant_id='unknown'`），它们不属于任何租户，因此**不会**
 * 出现在页面任何一族的数字里 —— 但它们确实消耗了 Token。不说明这一点，用户会以为
 * 「账单和页面为什么对不上」。这条提示只做解释：**不和任何 KPI 相加，也不进图表**。
 *
 * 几处刻意的取舍：
 * - **没有未归因调用时不渲染**（不是渲染一条 0）：常态是干净的，长期挂一条「0 次」
 *   只会变成噪音，最后所有人都学会无视它。
 * - **取数失败时不渲染**：它只是解释性提示，为它弹一条错误会把正常可用的页面弄脏；
 *   该分区的错误已由各自的 `ModuleError` 暴露。
 * - **占比只在后端给了才显示**：后端只对平台管理员下发占比（分母是全平台总量），
 *   其余角色拿到 null，此时只讲绝对量。
 */
export default function UnattributedNotice({ window, refreshKey, manualRefreshKey }: PmPaneProps) {
  const { t } = useTranslation();

  const load = useCallback(
    () => getUnattributedSummary(window),
    // manualRefreshKey 不进函数体，但必须进依赖：手动刷新要看得见地重取
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, manualRefreshKey],
  );
  const { data, error } = useAsyncData(load, { silentKey: refreshKey });

  if (error || !data || data.callCount <= 0) return null;

  const params = {
    calls: fmtCount(data.callCount),
    tokens: fmtCompact(data.totalTokens),
    last: fmtPmTime(data.lastSeenAt, PM_TIME_PATTERN.full) ?? '-',
  };

  // 占比较小（如 0.02%）时 1 位小数会显示成 0.0%，那比不显示更误导 —— 故只在
  // 拿到占比且不为 0 时拼接，并用两位小数（与同页其它百分比同口径）。
  const shareText =
    data.shareOfCalls != null && data.shareOfTokens != null
      ? t('platformMonitor.unattributed.share', {
          shareCalls: fmtPercent(data.shareOfCalls),
          shareTokens: fmtPercent(data.shareOfTokens),
        })
      : '';

  return (
    <Alert
      type="info"
      showIcon
      message={t('platformMonitor.unattributed.title')}
      description={
        <>
          {t('platformMonitor.unattributed.description', params)}
          {shareText ? ` ${shareText}` : ''}
        </>
      }
    />
  );
}
