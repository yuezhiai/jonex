/** 诊断抽屉归因段·逐失败行修复卡（2026-10-14 失败行修复线）：部分答上（N 源中 M 源）
 * 时，为每条 failed 观测行给出完整原因 + 行级归因 action + 按行 kind 分流的对症工具
 * ——概念别名 / 指标别名 / 纯文案（T/S 类无采纳动作）。
 * 归因由桥接逐行算好下发（规则表单一出处），本组件不做 reason→工具映射。 */
import { Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { ReplayObservation } from '../../../api/evolution';
import ConceptAdoptForm from './ConceptAdoptForm';
import MetricAdoptForm from './MetricAdoptForm';

interface Props {
  row: ReplayObservation;
  kb: string;
  feedbackId: string;
}

export default function FailedRowRepairCard({ row, kb, feedbackId }: Props) {
  const { t } = useTranslation();
  const kind = row.attribution?.kind;
  const shortDs = row.data_source_id ? row.data_source_id.slice(0, 8) : '—';
  return (
    <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: '8px 10px', marginBottom: 8 }}>
      <Space size={6} wrap style={{ marginBottom: 6 }}>
        <Tag>{row.anchor || '—'}</Tag>
        <Typography.Text type="secondary" title={row.data_source_id ?? undefined}>{shortDs}</Typography.Text>
      </Space>
      {row.reason && (
        <div style={{ marginBottom: 6 }}>
          <Typography.Text type="secondary">
            {t('evolutionConsole.detailReason')}：
          </Typography.Text>
          <Typography.Text>{row.reason}</Typography.Text>
        </div>
      )}
      {row.attribution?.action && (
        <p style={{ margin: '0 0 8px' }}>{row.attribution.action}</p>
      )}
      {kind === 'concept_gap' && <ConceptAdoptForm kb={kb} feedbackId={feedbackId} />}
      {kind === 'columns_or_convention_gap' && (
        <>
          <div style={{ marginBottom: 4 }}><MetricAdoptForm kb={kb} /></div>
          <Typography.Text type="secondary">
            {t('evolutionConsole.failedRowRepairConventionHint')}
          </Typography.Text>
        </>
      )}
    </div>
  );
}
