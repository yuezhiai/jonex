/**
 * W2.6 资源视图顶层页签：业务域 / 责任 / 落标 / 原文锚点。
 *
 * 子页签写入 URL（`resourceTab`）；外层页签由父组件 state 控制（未入 URL）。
 * 下钻走 `onDrillDown` 切回资产目录并应用筛选。
 */
import { Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';

import DomainViewPanel from './DomainViewPanel';
import ResponsibilityPanel from './ResponsibilityPanel';
import LandingViewPanel from './LandingViewPanel';
import SourceAnchorsPanel from './SourceAnchorsPanel';

export interface ResourceDrillDownFilters {
  spaceId?: string;
  kbId?: string;
  domain?: string;
  owner?: string;
}

interface Props {
  canWrite: boolean;
  onDrillDown: (filters: ResourceDrillDownFilters) => void;
}

const TABS = ['domains', 'responsibilities', 'landing', 'anchors'] as const;
type ResourceTabKey = (typeof TABS)[number];

export default function ResourceViewsTab({ canWrite, onDrillDown }: Props) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const requested = params.get('resourceTab');
  const activeKey: ResourceTabKey = TABS.includes(requested as ResourceTabKey)
    ? (requested as ResourceTabKey)
    : 'domains';

  return (
    <Tabs
      activeKey={activeKey}
      onChange={(key) => {
        const next = new URLSearchParams(params);
        next.set('resourceTab', key);
        setParams(next, { replace: true });
      }}
      items={[
        {
          key: 'domains',
          label: t('dataGovernance.resourceViews.tabs.domains'),
          children: <DomainViewPanel onDrillDown={onDrillDown} />,
        },
        {
          key: 'responsibilities',
          label: t('dataGovernance.resourceViews.tabs.responsibilities'),
          children: <ResponsibilityPanel onDrillDown={onDrillDown} />,
        },
        {
          key: 'landing',
          label: t('dataGovernance.resourceViews.tabs.landing'),
          children: <LandingViewPanel canWrite={canWrite} />,
        },
        {
          key: 'anchors',
          label: t('dataGovernance.resourceViews.tabs.anchors'),
          children: <SourceAnchorsPanel />,
        },
      ]}
    />
  );
}
