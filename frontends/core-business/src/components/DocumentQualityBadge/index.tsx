import React from 'react';
import { Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import type { QualityVerdict } from '@/types/domainKnowledge';

export interface DocumentQualityBadgeProps {
  /** 是否支持解析质量观测；false/缺省 = 该文档没有解析质量观测。 */
  supported?: boolean;
  /** 档位；null = 已跑但未产出报告 / 未评估。 */
  verdict?: QualityVerdict | null;
  /** 档位为 bad 时的判据名（如 page_yield_zero）。 */
  reasons?: string[];
}

/**
 * 解析质量档位徽标。
 *
 * ⚠ 三档语义不同，**不要合并**：
 * - `supported=false` → `—`（不是「待评估」；这份文档压根没有质量观测）
 * - `verdict=null` → 「待评估」（跑了但没产报告，或报告还没回来）
 * - `ok` **不是**「文档质量好」：本期只标定两条窄判据，见报告卡的 calibrated_rules。
 */
export default function DocumentQualityBadge({
  supported,
  verdict,
  reasons,
}: DocumentQualityBadgeProps) {
  const { t } = useTranslation();

  if (!supported) {
    return <span style={{ color: '#cbd5e1' }}>—</span>;
  }

  if (verdict === 'ok') {
    return (
      <span style={{ color: '#16a34a', fontSize: 12, fontWeight: 500 }}>
        {t('domainKnowledge.quality.ok')}
      </span>
    );
  }

  if (verdict === 'bad') {
    const tip = (reasons || [])
      .map((r) =>
        t(`domainKnowledge.quality.reasons.${r}`, { defaultValue: r }),
      )
      .join('；');
    return (
      <Tooltip title={tip || undefined}>
        <span style={{ color: '#dc2626', fontSize: 12, fontWeight: 500 }}>
          {t('domainKnowledge.quality.bad')}
        </span>
      </Tooltip>
    );
  }

  return (
    <span style={{ color: '#94a3b8', fontSize: 12 }}>
      {t('domainKnowledge.quality.unknown')}
    </span>
  );
}
