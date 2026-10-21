import { useCallback } from 'react';
import { Alert, Button, Descriptions, Drawer, Empty, Skeleton } from 'antd';
import { ArrowRightOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getCallObjectDetail } from '../../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import { fmtCount, fmtLatencyOrDash, fmtPercentOrDash } from '../format';
import { isNotFound } from '../errorText';
import { enumLabel } from '../labels';
import ModuleError from './ModuleError';
import type { PmCallProtocol, PmWindow } from '../types';

interface Props {
  open: boolean;
  objectId: string | null;
  protocol: PmCallProtocol | null;
  window: PmWindow;
  /** 跳转到调用记录页并按当前对象精确筛选；缺省时不显示入口 */
  onViewAllCalls?: () => void;
  onClose: () => void;
}

/**
 * C9 对象 / 能力详情抽屉。
 *
 * 契约里「对象不存在」与「对象存在但窗口内无任何调用」**共用 404 + 1002**，
 * 两者对用户是同一件事（这个窗格里没这个对象的数据），所以合并按「没查到」呈现。
 */
export default function ObjectDetailDrawer({
  open,
  objectId,
  protocol,
  window,
  onViewAllCalls,
  onClose,
}: Props) {
  const { t } = useTranslation();

  // 抽屉关着不发请求：组件一直挂着（只切 open），不挡的话关着也会打接口
  const load = useCallback(
    () =>
      open && objectId
        ? getCallObjectDetail(objectId, window, protocol ?? undefined)
        : Promise.resolve(null),
    [open, objectId, window, protocol],
  );
  const { data: detail, loading, error, reload } = useAsyncData(load);

  return (
    <Drawer
      size={600}
      open={open}
      onClose={onClose}
      title={objectId ?? ''}
      destroyOnHidden
      // 只有详情真正取到时才给入口：404 / 失败态下按这个对象筛记录页只会是一片空
      footer={
        detail && onViewAllCalls ? (
          <Button type="primary" block icon={<ArrowRightOutlined />} onClick={onViewAllCalls}>
            {t('platformMonitor.objectDetail.viewAllCalls')}
          </Button>
        ) : null
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : error ? (
        isNotFound(error) ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.objectDetail.notFound')} />
        ) : (
          <ModuleError error={error} onRetry={reload} />
        )
      ) : detail ? (
        <>
          {detail.metricMode === 'degraded' ? (
            <Alert
              type="info"
              showIcon
              className="yx-pm-drawer-alert"
              message={t('platformMonitor.objectDetail.degradedMode.title')}
              description={t('platformMonitor.objectDetail.degradedMode.description')}
            />
          ) : null}

          <Descriptions
            column={2}
            size="small"
            bordered
            items={[
              { key: 'name', label: t('platformMonitor.objectDetail.objectId'), children: detail.objectId },
              {
                key: 'protocol',
                label: t('platformMonitor.call.column.protocol'),
                // C9 的响应体不含 protocol（它是入参），显示的是用户点进来的那一行的协议
                children: enumLabel(t, 'platformMonitor.callProtocol', protocol),
              },
              { key: 'calls', label: t('platformMonitor.call.column.calls'), children: fmtCount(detail.callCount) },
              {
                key: 'successRate',
                label: t('platformMonitor.call.column.successRate'),
                children: fmtPercentOrDash(detail.successRate),
              },
              {
                key: 'latency',
                label: t('platformMonitor.call.column.avgLatency'),
                children: fmtLatencyOrDash(detail.avgLatencyMs),
              },
              {
                key: 'failed',
                label: t('platformMonitor.call.column.failures'),
                children: fmtCount(detail.failedCount),
              },
            ]}
          />

          <div className="yx-pm-drawer-block">
            <h4 className="yx-pm-drawer-title">{t('platformMonitor.objectDetail.topSources')}</h4>
            {detail.topSources.length ? (
              <ul className="yx-pm-drawer-list">
                {detail.topSources.map((item) => (
                  <li key={item.name} className="yx-pm-drawer-row">
                    <span className="yx-pm-cell-strong">{item.name}</span>
                    <span className="yx-pm-drawer-count">{fmtCount(item.callCount)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="yx-pm-drawer-empty">{t('platformMonitor.empty.noData')}</p>
            )}
          </div>

          <div className="yx-pm-drawer-block">
            <h4 className="yx-pm-drawer-title">{t('platformMonitor.objectDetail.recentFailures')}</h4>
            {detail.recentFailures.length ? (
              <ul className="yx-pm-drawer-list">
                {detail.recentFailures.map((item) => (
                  <li key={item.requestId} className="yx-pm-drawer-row">
                    <div className="yx-pm-drawer-failure">
                      <span className="yx-pm-drawer-failure-id">{item.requestId}</span>
                      <span className="yx-pm-drawer-failure-msg">{item.errorMessage ?? '-'}</span>
                    </div>
                    <span className="yx-pm-drawer-failure-time">{item.time ?? '-'}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="yx-pm-drawer-empty">{t('platformMonitor.empty.noData')}</p>
            )}
          </div>
        </>
      ) : null}
    </Drawer>
  );
}
