import { Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import {
  pmFailureDrillParams,
  pmObjectCallsDrillParams,
  toPmCallSubKey,
} from '../../constants/platformMonitor';
import type { PmCallScope } from '../../api/platformMonitor';
import type { PmCallProtocol, PmPaneProps } from './types';
import CallScopeView from './components/CallScopeView';
import CallRecords from './CallRecords';

/**
 * 调用监控：三个二级页签。
 *
 * 二级页签落在 `?sub=`，与一级的 `?tab=` 同在 query 上，所以改 sub 时要**合并**而不是覆盖，
 * 否则会把 tab 抹掉、退回任务监控。
 */
export default function CallMonitor(props: PmPaneProps) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const sub = toPmCallSubKey(searchParams.get('sub'));

  const handleChange = (key: string) => {
    // 只改 sub：筛选条件也是 URL 的一部分（记录页把它们写回这里），
    // 重建参数会把用户已设的筛选抹掉
    const next = new URLSearchParams(searchParams);
    next.set('sub', key);
    setSearchParams(next);
  };

  /** 失败数下钻：切到记录页签，并带上当前口径的筛选（参数拼装见 `pmFailureDrillParams`） */
  const drillToFailures = (scope: PmCallScope) => {
    setSearchParams(pmFailureDrillParams(scope, searchParams));
  };

  /** 对象详情下钻：切到记录页签，按对象标识精确筛选 */
  const drillToObjectCalls = (objectId: string, protocol: PmCallProtocol | null) => {
    setSearchParams(pmObjectCallsDrillParams(objectId, protocol, searchParams));
  };

  return (
    <Tabs
      className="yx-pm-subtabs"
      activeKey={sub}
      onChange={handleChange}
      destroyOnHidden
      items={[
        {
          key: 'external',
          label: t('platformMonitor.call.sub.external'),
          children: (
            <CallScopeView
              scope="target"
              {...props}
              onDrillFailures={() => drillToFailures('target')}
              onDrillObjectCalls={drillToObjectCalls}
            />
          ),
        },
        {
          key: 'capability',
          label: t('platformMonitor.call.sub.capability'),
          children: (
            <CallScopeView
              scope="capability"
              {...props}
              onDrillFailures={() => drillToFailures('capability')}
              onDrillObjectCalls={drillToObjectCalls}
            />
          ),
        },
        {
          key: 'records',
          label: t('platformMonitor.call.sub.records'),
          children: <CallRecords {...props} />,
        },
      ]}
    />
  );
}
