import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getDocumentStats } from '@/api/domainKnowledge';
import type { DocumentStatsResult } from '@/types/domainKnowledge';

export interface DocumentStatsBarProps {
  knowledgeBaseId: string;
  /** 外部变化时（上传/删除/重试后）递增以触发刷新。 */
  reloadFlag?: number;
}

interface Metric {
  key: keyof DocumentStatsResult;
  labelKey: string;
  color: string;
}

// 互斥四桶（处理中+已完成+编译失败+解析失败=总计），与后端 documents_stats、设计 §5 同源。
// 失败类指标常驻显示（即使为 0），便于随时看到指标存在。
const METRICS: Metric[] = [
  { key: 'total', labelKey: 'docPhase.total', color: 'var(--yx-brand-dark)' },
  { key: 'processing', labelKey: 'docPhase.processing', color: 'var(--yx-accent)' },
  { key: 'completed', labelKey: 'docPhase.completedStat', color: '#10b981' },
  { key: 'compileFailed', labelKey: 'docPhase.compileFailedStat', color: '#f59e0b' },
  { key: 'parseFailed', labelKey: 'docPhase.parseFailedStat', color: '#ef4444' },
];

/**
 * 文档处理状态统计栏。只表达「可用性」主轴 + 需关注项，不制造健康率假象。
 */
export default function DocumentStatsBar({ knowledgeBaseId, reloadFlag = 0 }: DocumentStatsBarProps) {
  const { t } = useTranslation();
  const [stats, setStats] = useState<DocumentStatsResult | null>(null);

  useEffect(() => {
    if (!knowledgeBaseId) return;
    let alive = true;
    getDocumentStats(knowledgeBaseId)
      .then((s) => {
        if (alive) setStats(s);
      })
      .catch(() => {
        /* 统计失败静默降级，不打扰主流程 */
      });
    return () => {
      alive = false;
    };
  }, [knowledgeBaseId, reloadFlag]);

  if (!stats) return null;

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap' }}>
      {METRICS.map((m) => {
        const value = stats[m.key];
        return (
          <span key={m.key} style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6 }}>
            <span style={{ fontSize: 20, fontWeight: 700, color: m.color, lineHeight: 1 }}>{value}</span>
            <span style={{ fontSize: 12, color: 'var(--yx-text-secondary)' }}>{t(m.labelKey)}</span>
          </span>
        );
      })}
    </div>
  );
}
