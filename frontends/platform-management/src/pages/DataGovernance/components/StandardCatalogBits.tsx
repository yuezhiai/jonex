import { Space, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DgDocStats } from '../standardTypes';
import { docStatsParts, hasScenarioMismatch } from '../standardUtils';

interface BindingStatusTagProps {
  bindingStatus: string;
  bindingScenarioId: string | null;
  compiledScenarioId: string | null;
}

/**
 * 绑定状态 Tag：绑定场景与编译场景不一致时追加 mismatch 提示。
 *
 * 场景影响表、知识库应用列表与 KB 详情抽屉三处共用，避免判定/配色漂移。
 */
export function BindingStatusTag({
  bindingStatus,
  bindingScenarioId,
  compiledScenarioId,
}: BindingStatusTagProps) {
  const { t } = useTranslation();
  return (
    <Space size={[0, 4]} wrap>
      <Tag>
        {bindingStatus && bindingStatus !== 'none'
          ? bindingStatus
          : t('dataGovernance.standards.noBinding')}
      </Tag>
      {hasScenarioMismatch({ bindingScenarioId, compiledScenarioId }) && (
        <Tag color="red">{t('dataGovernance.standards.mismatch')}</Tag>
      )}
    </Space>
  );
}

/** 文档统计单元格：四个分桶 + 本体就绪（五项都展示，0 也是真实统计值）。 */
export function DocStatsCell({ stats }: { stats: DgDocStats }) {
  const { t } = useTranslation();
  const parts = docStatsParts(stats);
  return (
    <Space direction="vertical" size={0} style={{ fontSize: 12 }}>
      <span>
        {t('dataGovernance.standards.docTotal')}: {parts.total}
      </span>
      <span>
        {t('dataGovernance.standards.docNoTarget')}: {parts.noTarget} /{' '}
        {t('dataGovernance.standards.docNotApplied')}: {parts.notApplied}
      </span>
      <span>
        {t('dataGovernance.standards.docAppliedCurrent')}: {parts.appliedCurrent} /{' '}
        {t('dataGovernance.standards.docOntologyReady')}: {stats.ontologyReady}
      </span>
    </Space>
  );
}
