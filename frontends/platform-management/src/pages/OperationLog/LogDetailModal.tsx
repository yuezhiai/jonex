import React from 'react';
import { Descriptions, Modal, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import type { AuditLogItem, AuditActionOption, AuditResourceType } from '../../api/auditLogs';
import { getActionLabel, actionTagColor, getResourceLabel, formatDuration } from './index';

interface LogDetailModalProps {
  open: boolean;
  detailItem: AuditLogItem | null;
  onClose: () => void;
  actionOptions: AuditActionOption[];
  resourceOptions: AuditResourceType[];
  locale: string;
}

export default function LogDetailModal({
  open,
  detailItem,
  onClose,
  actionOptions,
  resourceOptions,
  locale,
}: LogDetailModalProps) {
  const { t } = useTranslation();

  // 是否失败：outcome 明确为 FAILED，或未提供 outcome 时按 HTTP 状态码 >= 400 兜底判断
  const isFailed =
    !!detailItem &&
    (detailItem.outcome === 'FAILED' || (!detailItem.outcome && (detailItem.status_code ?? 0) >= 400));

  const items = detailItem
    ? [
        {
          key: 'status',
          label: t('operationLog.statusLabel'),
          children: (
            <Tag color={isFailed ? 'red' : 'success'}>
              {isFailed ? t('operationLog.failed') : t('operationLog.success')}
            </Tag>
          ),
        },
        ...(detailItem.status_code != null
          ? [{ key: 'statusCode', label: t('operationLog.statusCodeLabel'), children: <span>{detailItem.status_code}</span> }]
          : []),
        {
          key: 'action',
          label: t('operationLog.actionLabel'),
          children: (
            <Tag color={actionTagColor(detailItem.action)}>
              {getActionLabel(locale, actionOptions, detailItem.action, t('operationLog.unknownAction'))}
            </Tag>
          ),
        },
        {
          key: 'resource',
          label: t('operationLog.resourceLabel'),
          children: (
            <span>
              {getResourceLabel(
                detailItem.resource,
                detailItem.resource_name,
                detailItem.resource_id,
                resourceOptions,
                locale,
              )}
              {detailItem.resource_name && (
                <span style={{ fontSize: 12, color: '#94a3b8' }}> · {detailItem.resource_name}</span>
              )}
              {detailItem.resource_id && (
                <span style={{ fontSize: 11, color: '#94a3b8', marginLeft: 8 }}>{detailItem.resource_id}</span>
              )}
            </span>
          ),
        },
        {
          key: 'user',
          label: t('operationLog.userLabel'),
          children: <span>{detailItem.username || '--'}</span>,
        },
        { key: 'ip', label: t('operationLog.ipLabel'), children: <span>{detailItem.ip || '--'}</span> },
        {
          key: 'duration',
          label: t('operationLog.durationLabel'),
          children: <span>{formatDuration(detailItem.duration_ms)}</span>,
        },
        {
          key: 'time',
          label: t('operationLog.timeLabel'),
          children: <span>{detailItem.created_at ? dayjs(detailItem.created_at).format('YYYY-MM-DD HH:mm:ss') : '--'}</span>,
        },
        { key: 'traceId', label: t('operationLog.traceIdLabel'), children: <span>{detailItem.trace_id || '--'}</span> },
        ...(isFailed && detailItem.error_message
          ? [
              {
                key: 'errorMessage',
                label: t('operationLog.errorMessageLabel'),
                span: 2,
                children: <span style={{ whiteSpace: 'pre-line' }}>{detailItem.error_message}</span>,
              },
            ]
          : []),
        ...(isFailed && detailItem.error_stack
          ? [
              {
                key: 'errorStack',
                label: t('operationLog.errorStackLabel'),
                span: 2,
                children: (
                  <pre
                    style={{
                      margin: 0,
                      background: '#f8fafc',
                      padding: 12,
                      borderRadius: 8,
                      fontSize: 12,
                      maxHeight: 300,
                      overflow: 'auto',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                    }}
                  >
                    {detailItem.error_stack}
                  </pre>
                ),
              },
            ]
          : []),
      ]
    : [];

  return (
    <Modal title={t('operationLog.logDetail')} open={open} onCancel={onClose} footer={null} width={600}>
      {detailItem && (
        <Descriptions size="small" column={2} items={items} />
      )}
    </Modal>
  );
}
