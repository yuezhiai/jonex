import { Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';

import QualityIssuesPanel from './QualityIssuesPanel';
import QualityRulesPanel from './QualityRulesPanel';
import QualityRunsPanel from './QualityRunsPanel';

interface Props {
  canWrite: boolean;
  userId: string | null;
  isAdmin: boolean;
}

const TABS = ['rules', 'runs', 'issues'] as const;
type QualityTabKey = (typeof TABS)[number];

export default function QualityTab({ canWrite, userId, isAdmin }: Props) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const requested = params.get('qualityTab');
  const activeKey: QualityTabKey = TABS.includes(requested as QualityTabKey)
    ? (requested as QualityTabKey)
    : 'rules';

  return (
    <Tabs
      activeKey={activeKey}
      onChange={(key) => {
        const next = new URLSearchParams(params);
        next.set('qualityTab', key);
        setParams(next, { replace: true });
      }}
      items={[
        {
          key: 'rules',
          label: t('dataGovernance.quality.tabs.rules'),
          children: <QualityRulesPanel canWrite={canWrite} />,
        },
        {
          key: 'runs',
          label: t('dataGovernance.quality.tabs.runs'),
          children: <QualityRunsPanel canWrite={canWrite} />,
        },
        {
          key: 'issues',
          label: t('dataGovernance.quality.tabs.issues'),
          children: (
            <QualityIssuesPanel
              canWrite={canWrite}
              userId={userId}
              isAdmin={isAdmin}
            />
          ),
        },
      ]}
    />
  );
}
