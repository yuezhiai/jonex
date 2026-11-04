/**
 * 知识库设置 →「治理」页签（P1：资产标签 / 策略 / 用户密级 / 审计）。
 *
 * 四个子视图各自取数、各自处理四态；写入口统一由 `canWrite` 门控。
 */
import { useState } from 'react';
import { Segmented } from 'antd';
import { useTranslation } from 'react-i18next';
import LabelsView from './LabelsView';
import PoliciesView from './PoliciesView';
import ClearanceView from './ClearanceView';
import AuditView from './AuditView';
import SensitiveScanView from './SensitiveScanView';
import ApprovalsView from './ApprovalsView';

type GovernanceView = 'labels' | 'policies' | 'clearance' | 'audit' | 'scan'
  | 'approvals';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

export default function GovernanceTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [view, setView] = useState<GovernanceView>('labels');

  return (
    <div>
      <Segmented
        value={view}
        onChange={(value) => setView(value as GovernanceView)}
        options={[
          { value: 'labels', label: t('domainKnowledge.governance.viewLabels') },
          { value: 'policies', label: t('domainKnowledge.governance.viewPolicies') },
          { value: 'clearance', label: t('domainKnowledge.governance.viewClearance') },
          { value: 'audit', label: t('domainKnowledge.governance.viewAudit') },
          { value: 'scan', label: t('domainKnowledge.governance.viewScan') },
          { value: 'approvals', label: t('domainKnowledge.governance.viewApprovals') },
        ]}
      />
      <div style={{ marginTop: 12 }}>
        {view === 'labels' && <LabelsView kbId={kbId} canWrite={canWrite} />}
        {view === 'policies' && <PoliciesView kbId={kbId} canWrite={canWrite} />}
        {view === 'clearance' && <ClearanceView kbId={kbId} canWrite={canWrite} />}
        {view === 'audit' && <AuditView kbId={kbId} />}
        {view === 'scan' && <SensitiveScanView kbId={kbId} canWrite={canWrite} />}
        {view === 'approvals' && <ApprovalsView kbId={kbId} canWrite={canWrite} />}
      </div>
    </div>
  );
}
