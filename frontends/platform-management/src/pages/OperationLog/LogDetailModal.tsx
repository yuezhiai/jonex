import { useState } from 'react';
import { Button, Modal, message } from 'antd';
import { CheckCircleFilled, CloseCircleFilled, CopyOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import copyToClipboard from 'copy-to-clipboard';
import type { AuditLogItem, AuditActionOption, AuditResourceType } from '../../api/auditLogs';
import { getActionLabel, getResourceLabel, formatDuration } from './index';
import './LogDetailModal.css';

interface LogDetailModalProps {
  open: boolean;
  detailItem: AuditLogItem | null;
  onClose: () => void;
  actionOptions: AuditActionOption[];
  resourceOptions: AuditResourceType[];
  locale: string;
}

/** 把接口返回的 request_params / response_body 统一转成可读文本 */
function toReadable(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 代码块：超过阈值先折叠，底部渐隐 + 「展开」；不做卡片，只有一层浅底 */
function CodeBlock({ text }: { text: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const collapsible = text.split('\n').length > 12 || text.length > 900;
  const collapsed = collapsible && !expanded;

  return (
    <div className={`lod-code${collapsed ? ' is-collapsed' : ''}`}>
      <pre className="lod-pre">{text}</pre>
      {collapsible && (
        <Button type="link" size="small" className="lod-more" onClick={() => setExpanded((prev) => !prev)}>
          {collapsed ? t('operationLog.expand') : t('operationLog.collapse')}
        </Button>
      )}
    </div>
  );
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

  const copyText = async (text: string | null | undefined): Promise<void> => {
    if (!text) return;
    // 线上是 http 非安全上下文，clipboard API 不可用，copy-to-clipboard 内部自带 execCommand 降级
    if (await copyToClipboard(text)) message.success(t('common.copySuccess'));
    else message.error(t('common.copyFailed'));
  };

  const resourceLabel = detailItem
    ? getResourceLabel(detailItem.resource, detailItem.resource_name, detailItem.resource_id, resourceOptions, locale)
    : '';
  const actionLabel = detailItem
    ? getActionLabel(locale, actionOptions, detailItem.action, t('operationLog.unknownAction'))
    : '';

  const requestText = toReadable(detailItem?.request_params);
  const responseText = toReadable(detailItem?.response_body);
  const hasRequestLine = !!(detailItem?.method || detailItem?.path);
  const hasTechInfo = !!(
    detailItem?.trace_id ||
    detailItem?.resource_id ||
    detailItem?.service_name ||
    detailItem?.log_type
  );

  /** 头部一行元信息：时间 · 用户 · IP · 耗时 · HTTP 状态码 */
  const metaText = detailItem
    ? [
        detailItem.created_at ? dayjs(detailItem.created_at).format('YYYY-MM-DD HH:mm:ss') : '--',
        detailItem.username || '--',
        detailItem.ip || '--',
        formatDuration(detailItem.duration_ms),
        detailItem.status_code != null ? `HTTP ${detailItem.status_code}` : null,
      ]
        .filter(Boolean)
        .join(' · ')
    : '';

  return (
    <Modal
      width={840}
      title={t('operationLog.logDetail')}
      open={open}
      onCancel={onClose}
      footer={null}
      // 内容再长也不把整页顶出去：弹窗标题固定，正文区在视口高度内自己滚
      styles={{ body: { paddingTop: 10, maxHeight: 'calc(100vh - 140px)', overflowY: 'auto' } }}
    >
      {detailItem && (
        <div className="lod">
          {/* 头部：谁、做了什么、什么结果 */}
          <div className={`lod-head ${isFailed ? 'is-failed' : 'is-ok'}`}>
            <span className="lod-head-icon">{isFailed ? <CloseCircleFilled /> : <CheckCircleFilled />}</span>
            <div className="lod-head-main">
              <div className="lod-head-title">
                <span className="lod-head-action">{actionLabel}</span>
                <span className="lod-head-sep">/</span>
                <span className="lod-head-res">{resourceLabel}</span>
              </div>
              <div className="lod-head-meta">{metaText}</div>
            </div>
            <span className="lod-head-status">{isFailed ? t('operationLog.failed') : t('operationLog.success')}</span>
          </div>

          {/* 请求：方法 + 路径，有入参再跟一段 JSON */}
          {(hasRequestLine || requestText) && (
            <section className="lod-sec">
              <div className="lod-sec-head">
                <span className="lod-sec-title">{t('operationLog.requestInfo')}</span>
                {requestText && (
                  <Button
                    type="text"
                    size="small"
                    className="lod-copy-text"
                    icon={<CopyOutlined />}
                    onClick={() => void copyText(requestText)}
                  >
                    {t('common.copy')}
                  </Button>
                )}
              </div>
              {hasRequestLine && (
                <div className="lod-req">
                  {detailItem.method && <span className="lod-method">{detailItem.method}</span>}
                  {detailItem.path && <span className="lod-path">{detailItem.path}</span>}
                </div>
              )}
              {requestText ? (
                <CodeBlock text={requestText} />
              ) : (
                <div className="lod-hint">{t('operationLog.noRequestParams')}</div>
              )}
            </section>
          )}

          {/* 响应：状态码已在头部，这里放响应体 */}
          {responseText && (
            <section className="lod-sec">
              <div className="lod-sec-head">
                <span className="lod-sec-title">{t('operationLog.responseBody')}</span>
                <Button
                  type="text"
                  size="small"
                  className="lod-copy-text"
                  icon={<CopyOutlined />}
                  onClick={() => void copyText(responseText)}
                >
                  {t('common.copy')}
                </Button>
              </div>
              <CodeBlock text={responseText} />
            </section>
          )}

          {/* 异常：原因 + 堆栈 */}
          {(detailItem.error_message || detailItem.error_stack) && (
            <section className="lod-sec is-error">
              <div className="lod-sec-head">
                <span className="lod-sec-title">{t('operationLog.exception')}</span>
                {detailItem.error_stack && (
                  <Button
                    type="text"
                    size="small"
                    className="lod-copy-text"
                    icon={<CopyOutlined />}
                    onClick={() => void copyText(detailItem.error_stack)}
                  >
                    {t('common.copy')}
                  </Button>
                )}
              </div>
              {detailItem.error_message && <div className="lod-error-msg">{detailItem.error_message}</div>}
              {detailItem.error_stack && <CodeBlock text={detailItem.error_stack} />}
            </section>
          )}

          {/* 技术信息：排查时才看的字段，放最后 */}
          {hasTechInfo && (
            <section className="lod-sec">
              <div className="lod-sec-head">
                <span className="lod-sec-title">{t('operationLog.techInfo')}</span>
              </div>
              <div className="lod-rows">
                {detailItem.resource_id && (
                  <div className="lod-row">
                    <span className="lod-row-label">{t('operationLog.resource')}</span>
                    <span className="lod-row-value">{detailItem.resource_id}</span>
                  </div>
                )}
                {detailItem.service_name && (
                  <div className="lod-row">
                    <span className="lod-row-label">{t('operationLog.serviceName')}</span>
                    <span className="lod-row-value">{detailItem.service_name}</span>
                  </div>
                )}
                {detailItem.log_type && (
                  <div className="lod-row">
                    <span className="lod-row-label">{t('operationLog.logType')}</span>
                    <span className="lod-row-value">{detailItem.log_type}</span>
                  </div>
                )}
                {detailItem.trace_id && (
                  <div className="lod-row lod-row-wide">
                    <span className="lod-row-label">{t('operationLog.traceIdLabel')}</span>
                    <span className="lod-row-value">
                      <span className="lod-mono">{detailItem.trace_id}</span>
                      <Button
                        type="text"
                        size="small"
                        className="lod-copy"
                        icon={<CopyOutlined />}
                        aria-label={t('common.copy')}
                        onClick={() => void copyText(detailItem.trace_id)}
                      />
                    </span>
                  </div>
                )}
              </div>
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}
