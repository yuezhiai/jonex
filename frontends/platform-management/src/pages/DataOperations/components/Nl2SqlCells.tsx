import { Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { DO_NL2SQL_UNASSIGNED_SOURCE } from '../types';

/** 与四个钻取抽屉一致：列表里的「信息缺失」也用破折号，但它是纯文本单元格 */
export const NL2SQL_DASH = '—';

/**
 * 数据源单元格：空源哨兵走 i18n「未识别源」，其余优先源名、富化不到回落 id。
 * `data_source_id` 在降级空壳里可为 null —— 不假设非空。
 */
export function SourceText({
  dataSourceId,
  sourceName,
}: {
  dataSourceId: string | null;
  sourceName?: string | null;
}) {
  const { t } = useTranslation();
  if (dataSourceId === DO_NL2SQL_UNASSIGNED_SOURCE) {
    return <span className="yx-do-muted">{t('dataOperations.nl2sql.filter.unassignedSource')}</span>;
  }
  return <span>{sourceName || dataSourceId || NL2SQL_DASH}</span>;
}

/** 结果标签：成功绿 / 失败红；null（降级）显示占位，不伪造成失败 */
export function OutcomeTag({ outcome }: { outcome: string | null }) {
  const { t } = useTranslation();
  if (!outcome) return <span className="yx-do-muted">{NL2SQL_DASH}</span>;
  const color =
    outcome === 'success' ? 'success' : outcome === 'failed' ? 'error' : 'default';
  const label =
    outcome === 'success'
      ? t('dataOperations.nl2sql.outcome.success')
      : outcome === 'failed'
        ? t('dataOperations.nl2sql.outcome.failed')
        : outcome;
  return <Tag color={color}>{label}</Tag>;
}

/** A1b 标签：只标「被预编译校验拦下并重试过」的行；没触发时给占位 */
export function A1bTag({ retried }: { retried: boolean }) {
  const { t } = useTranslation();
  if (!retried) return <span className="yx-do-muted">{NL2SQL_DASH}</span>;
  return <Tag color="warning">{t('dataOperations.nl2sql.a1b.retried')}</Tag>;
}

/** 失败码：空码归「未分类」（后端 group by 的空桶），未知码原样展示 */
export function ReasonCodeText({ code }: { code: string | null }) {
  const { t } = useTranslation();
  if (!code) return <span className="yx-do-muted">{t('dataOperations.nl2sql.reasonCode.unclassified')}</span>;
  return <Tag>{code}</Tag>;
}
