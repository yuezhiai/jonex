import { useCallback, useEffect, useState } from 'react';
import { Alert, Descriptions, Drawer, Empty, Result, Skeleton, Table, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { useTranslation } from 'react-i18next';
import { getCallAttempts, getCallDetail } from '../../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import { PM_RECORD_PAGE_SIZE } from '../../../constants/platformMonitor';
import { fmtCount, fmtLatencyOrDash } from '../format';
import { isNotFound } from '../errorText';
import { CallStatusTag } from './Tags';
import ModuleError from './ModuleError';
import type { PmCallAttempt } from '../types';

/** payload 是接口给的字符串，是 JSON 就缩进后再显示，不是就原样落出来 */
function pretty(raw: string | null): string {
  if (!raw) return '-';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

interface Props {
  open: boolean;
  requestId: string | null;
  /** 降级模式下同一 requestId 有多行，必须靠它定位；正常模式为 null，接口会忽略 */
  attemptId?: string | null;
  onClose: () => void;
}

/**
 * C7 调用详情 + C8 重试明细。
 *
 * 降级模式下 `request_id` 会重复，C7 要带 `attempt_id` 才定位得准 —— 该值由 C6 的记录行回显
 * （`PmCallRecord.attemptId`），列表点行时一路传进来。C8 的入参没有 `attempt_id`。
 *
 * 详情与 attempt 明细**分开取数**：翻 attempt 的页码不该把上面整块详情打回骨架屏，
 * 反过来详情挂了也不该让已经拿到的 attempt 消失。
 */
export default function CallDetailDrawer({ open, requestId, attemptId, onClose }: Props) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);

  // 换一条记录就回第一页：停在第 3 页而新记录只有 2 条 attempt 会是一片空白
  useEffect(() => {
    setPage(1);
  }, [requestId, attemptId]);

  // 抽屉关着不发请求：组件一直挂着（只切 open），不挡的话关着也会打接口
  const loadDetail = useCallback(
    () => (open && requestId ? getCallDetail(requestId, attemptId ?? undefined) : Promise.resolve(null)),
    [open, requestId, attemptId],
  );
  const {
    data: detail,
    loading: detailLoading,
    error: detailError,
    reload: reloadDetail,
  } = useAsyncData(loadDetail);

  const loadAttempts = useCallback(
    () => (open && requestId ? getCallAttempts(requestId, page, PM_RECORD_PAGE_SIZE) : Promise.resolve(null)),
    [open, requestId, page],
  );
  const {
    data: attemptsPage,
    loading: attemptsLoading,
    error: attemptsError,
    reload: reloadAttempts,
  } = useAsyncData(loadAttempts);

  const attempts = attemptsPage?.items ?? [];

  const attemptColumns: TableColumnsType<PmCallAttempt> = [
    { title: t('platformMonitor.callDetail.attempt.no'), dataIndex: 'attemptNo', width: 60, align: 'right' },
    {
      title: t('platformMonitor.callDetail.attempt.startedAt'),
      dataIndex: 'attemptStartedAt',
      render: (value: string | null) => value ?? '-',
    },
    {
      title: t('platformMonitor.callDetail.attempt.duration'),
      dataIndex: 'durationMs',
      align: 'right',
      render: fmtLatencyOrDash,
    },
    {
      title: t('platformMonitor.callDetail.attempt.status'),
      dataIndex: 'status',
      render: (value: PmCallAttempt['status']) => <CallStatusTag status={value} />,
    },
    {
      title: t('platformMonitor.callDetail.attempt.errorCode'),
      dataIndex: 'errorCode',
      render: (value: string | null) => value ?? '-',
    },
    {
      // 重试的成因几乎都在「上一次等太久了」，所以等待时长紧挨着错误信息放
      title: t('platformMonitor.callDetail.attempt.waitBefore'),
      dataIndex: 'waitBeforeMs',
      align: 'right',
      render: fmtLatencyOrDash,
    },
  ];

  return (
    <Drawer
      size={720}
      open={open}
      onClose={onClose}
      title={t('platformMonitor.callDetail.title')}
      destroyOnHidden
    >
      {detailLoading ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : detailError ? (
        isNotFound(detailError) ? (
          // 查不到是确定性结果，不给重试按钮：点了还是同一条必然失败的请求
          <Result
            status="404"
            title={t('platformMonitor.callDetail.notFound.title')}
            subTitle={t('platformMonitor.callDetail.notFound.description')}
          />
        ) : (
          <ModuleError error={detailError} onRetry={reloadDetail} />
        )
      ) : detail ? (
        <>
          {/* 本期 C7 是薄壳：物理表只落了 request_id / error_message，其余字段恒为 null。
              不说明的话，满屏「-」会被读成取数失败而不是「尚未采集」。 */}
          <Alert
            type="info"
            showIcon
            className="yx-pm-drawer-alert"
            message={t('platformMonitor.callDetail.thinShellTitle')}
            description={t('platformMonitor.callDetail.thinShellDescription')}
          />

          {detail.metricMode === 'degraded' ? (
            <Alert
              type="info"
              showIcon
              className="yx-pm-drawer-alert"
              message={t('platformMonitor.callDetail.degradedMode.title')}
              description={t('platformMonitor.callDetail.degradedMode.description')}
            />
          ) : null}

          <Descriptions
            column={2}
            size="small"
            bordered
            // 未采集的字段直接不占格：留一格「-」和「查询失败」长得一样
            items={[
              { key: 'requestId', label: 'Request ID', children: detail.requestId, span: 2 },
              ...(detail.method
                ? [{ key: 'method', label: t('platformMonitor.callDetail.method'), children: detail.method }]
                : []),
              ...(detail.statusCode != null
                ? [
                    {
                      key: 'statusCode',
                      label: t('platformMonitor.callDetail.statusCode'),
                      children: (
                        <Tag color={detail.statusCode >= 400 ? 'red' : 'green'}>{detail.statusCode}</Tag>
                      ),
                    },
                  ]
                : []),
              ...(detail.path
                ? [{ key: 'path', label: t('platformMonitor.callDetail.path'), children: detail.path, span: 2 }]
                : []),
              ...(detail.stage != null
                ? [{ key: 'stage', label: t('platformMonitor.callDetail.stage'), children: detail.stage }]
                : []),
              {
                key: 'retry',
                label: t('platformMonitor.callDetail.retryCount'),
                // attempt 明细没取到时宁可不显示，也不报一个假的 0
                children: attemptsPage ? fmtCount(attemptsPage.retryCount) : '-',
              },
            ]}
          />

          {detail.errorMessage ? (
            <div className="yx-pm-drawer-block">
              <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.errorMessage')}</h4>
              <pre className="yx-pm-payload yx-pm-payload-error">{detail.errorMessage}</pre>
            </div>
          ) : null}

          {/* 三档 token 只有模型调用才有，全空时整段不出现，免得非模型调用挂一片「-」 */}
          {detail.totalTokens != null ? (
            <div className="yx-pm-drawer-block">
              <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.tokens')}</h4>
              <Descriptions
                column={3}
                size="small"
                bordered
                items={[
                  {
                    key: 'prompt',
                    label: t('platformMonitor.callDetail.promptTokens'),
                    children: detail.promptTokens ?? '-',
                  },
                  {
                    key: 'completion',
                    label: t('platformMonitor.callDetail.completionTokens'),
                    children: detail.completionTokens ?? '-',
                  },
                  {
                    key: 'total',
                    label: t('platformMonitor.callDetail.totalTokens'),
                    children: fmtCount(detail.totalTokens),
                  },
                ]}
              />
            </div>
          ) : null}

          {/* 报文同样按「有才渲染」处理：本期两者恒为 null，空块只会多两片「-」 */}
          {detail.requestPayload ? (
            <div className="yx-pm-drawer-block">
              <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.requestPayload')}</h4>
              <pre className="yx-pm-payload">{pretty(detail.requestPayload)}</pre>
            </div>
          ) : null}

          {detail.responsePayload ? (
            <div className="yx-pm-drawer-block">
              <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.responsePayload')}</h4>
              <pre className="yx-pm-payload">{pretty(detail.responsePayload)}</pre>
            </div>
          ) : null}

          {/* C7 的 meta 是采集元数据，后端已脱敏；本期通常为 null，有才展示 */}
          {detail.meta ? (
            <div className="yx-pm-drawer-block">
              <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.meta')}</h4>
              <pre className="yx-pm-payload">{JSON.stringify(detail.meta, null, 2)}</pre>
            </div>
          ) : null}

          <div className="yx-pm-drawer-block">
            <h4 className="yx-pm-drawer-title">{t('platformMonitor.callDetail.attempts')}</h4>
            {attemptsError ? (
              <ModuleError error={attemptsError} onRetry={reloadAttempts} />
            ) : (
              <Table
                size="small"
                rowKey="attemptId"
                loading={attemptsLoading}
                dataSource={attempts}
                columns={attemptColumns}
                locale={{
                  emptyText: (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description={t('platformMonitor.empty.noData')}
                    />
                  ),
                }}
                // C8 分页：重试次数理论上一页放得下，但契约给了 page/page_size 就得能翻
                pagination={{
                  current: page,
                  pageSize: PM_RECORD_PAGE_SIZE,
                  total: attemptsPage?.total ?? 0,
                  onChange: setPage,
                  showSizeChanger: false,
                  size: 'small',
                  hideOnSinglePage: true,
                }}
                scroll={{ x: 640 }}
              />
            )}
          </div>
        </>
      ) : null}
    </Drawer>
  );
}
