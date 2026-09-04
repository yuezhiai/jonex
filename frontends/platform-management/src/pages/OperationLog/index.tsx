import React, { useState, useEffect, useCallback } from 'react';
import { Input, Button, Table, Tag, Select, Space, Spin, Result, DatePicker } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import LogDetailModal from './LogDetailModal';
import {
  listAuditLogs,
  getAuditLog,
  listAuditActions,
  listAuditResourceTypes,
  type AuditLogItem,
  type AuditActionOption,
  type AuditResourceType,
} from '../../api/auditLogs';
const { RangePicker } = DatePicker;

/** 将原始 action 值转为可读短名（降级显示用）。 */
export function actionToLabel(action: string): string {
  if (!action) return action;
  const parts = action.split('.');
  const lastPart = parts[parts.length - 1];
  if (lastPart.includes('_')) {
    return lastPart
      .split('_')
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
  }
  if (parts.length > 1) return parts[parts.length - 1].toUpperCase();
  return action;
}

/** 后端两端均未提供翻译映射时视为未知 action。 */
export function isUnknownAction(opt: AuditActionOption): boolean {
  return opt.label_zh === opt.action && opt.label_en === opt.action;
}

/** 从 AuditActionOption 中获取当前语言下的显示名。 */
export function getOptionLabel(opt: AuditActionOption, locale: string, fallback?: string): string {
  const label = locale.startsWith('zh') ? opt.label_zh : opt.label_en;
  if (label === opt.action) {
    return isUnknownAction(opt) && fallback ? fallback : actionToLabel(opt.action);
  }
  return label;
}

/** 获取操作类型的显示名。 */
export function getActionLabel(
  locale: string,
  options: AuditActionOption[],
  action: string,
  fallback?: string,
): string {
  const found = options.find((o) => o.action === action);
  if (found) return getOptionLabel(found, locale, fallback);
  return fallback ?? actionToLabel(action);
}

/** 操作类型的标签颜色。 */
export function actionTagColor(action: string): string {
  if (action.includes('delete')) return 'red';
  if (action.includes('create') || action.includes('upload')) return 'green';
  if (action.includes('login')) return 'cyan';
  return 'default';
}

/** 获取资源类型的显示名。 */
export function getResourceLabel(
  resource: string | null,
  resource_name: string | null,
  resource_id: string | null,
  resourceOptions: AuditResourceType[],
  locale: string,
): string {
  const matched = resourceOptions.find((o) => o.resource === resource);
  if (matched) return locale.startsWith('zh') ? matched.label_zh : matched.label_en;
  return resource_name || resource_id || resource || '--';
}

/** 格式化耗时：>= 1 秒按秒展示，< 1 秒按毫秒展示。 */
export function formatDuration(durationMs: number | null | undefined): string {
  if (!durationMs) return '--';
  if (durationMs >= 1000) return `${(durationMs / 1000).toFixed(1)}s`;
  return `${durationMs}ms`;
}

export default function OperationLog() {
  const { t, i18n } = useTranslation();
  const [logs, setLogs] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [keyword, setKeyword] = useState('');
  const [action, setAction] = useState<string | undefined>();
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(null);
  const [detailItem, setDetailItem] = useState<AuditLogItem | null>(null);
  const [resource, setResource] = useState<string | undefined>();

  // ---- 操作类型下拉动态数据 ----
  const [actionOptions, setActionOptions] = useState<AuditActionOption[]>([]);
  const [actionsLoading, setActionsLoading] = useState(true);
  const [actionsError, setActionsError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setActionsLoading(true);
    setActionsError(false);
    listAuditActions()
      .then((opts) => {
        if (!cancelled) {
          const deduped = Array.from(new Map(opts.map((o) => [o.action, o])).values());
          setActionOptions(deduped);
        }
      })
      .catch(() => {
        if (!cancelled) setActionsError(true);
      })
      .finally(() => {
        if (!cancelled) setActionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- 资源类型下拉动态数据 ----
  const [resourceOptions, setResourceOptions] = useState<AuditResourceType[]>([]);
  const [resourcesLoading, setResourcesLoading] = useState(true);
  const [resourcesError, setResourcesError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setResourcesLoading(true);
    setResourcesError(false);
    listAuditResourceTypes()
      .then((opts) => {
        if (!cancelled) setResourceOptions(opts);
      })
      .catch(() => {
        if (!cancelled) setResourcesError(true);
      })
      .finally(() => {
        if (!cancelled) setResourcesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(
    async (p = 1, ps?: number, kw?: string) => {
      setLoading(true);
      setError(null);
      try {
        const r = await listAuditLogs({
          page: p,
          page_size: ps ?? pageSize,
          action: action || undefined,
          resource: resource || undefined,
          keyword: kw || keyword || undefined,
          start_time: dateRange?.[0]?.startOf('day').toISOString(),
          end_time: dateRange?.[1]?.endOf('day').toISOString(),
        });
        setLogs(r.items);
        setTotal(r.total);
        setPage(p);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : t('common.loadFailed'));
      } finally {
        setLoading(false);
      }
    },
    [action, resource, dateRange, keyword, pageSize, t],
  );

  useEffect(() => {
    load(1);
  }, [load]);

  const showDetail = async (id: number) => {
    try {
      const d = await getAuditLog(id);
      setDetailItem(d);
    } catch {
      /* ignore */
    }
  };

  const locale = i18n.language || 'zh-CN';

  const columns = [
    {
      title: t('operationLog.time'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string | null) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '--'),
    },
    { title: t('operationLog.user'), dataIndex: 'username', key: 'username', width: 90 },
    {
      title: t('operationLog.action'),
      dataIndex: 'action',
      key: 'action',
      width: 90,
      render: (v: string) => {
        const label = getActionLabel(locale, actionOptions, v, t('operationLog.unknownAction'));
        return <Tag color={actionTagColor(v)}>{label}</Tag>;
      },
    },
    {
      title: t('operationLog.resource'),
      dataIndex: 'resource',
      key: 'resource',
      width: 100,
      render: (_: unknown, r: AuditLogItem) => {
        const label = getResourceLabel(r.resource, r.resource_name, r.resource_id, resourceOptions, locale);
        return (
          <span>
            {label}
            {r.resource_name && (
              <div style={{ fontSize: 11, color: '#94a3b8', lineHeight: 1.4 }}>{r.resource_name}</div>
            )}
          </span>
        );
      },
    },
    { title: t('operationLog.ip'), dataIndex: 'ip', key: 'ip', width: 120 },
    {
      title: t('operationLog.duration'),
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 70,
      render: (v: number | null) => formatDuration(v),
    },
    {
      title: t('common.actions'),
      key: 'detail',
      width: 60,
      render: (_: unknown, r: AuditLogItem) => (
        <a className="yx-table-action" onClick={() => showDetail(r.id)}>
          {t('operationLog.detail')}
        </a>
      ),
    },
  ];

  if (error)
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => load(1)}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('operationLog.title')}</h1>
      </div>
      <div className="yx-card">
        <Space style={{ marginBottom: 16 }} wrap>
          <Input.Search
            prefix={<SearchOutlined />}
            placeholder={t('operationLog.searchLog')}
            style={{ width: 180 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={(v) => load(1, undefined, v)}
            onClear={() => load(1, undefined, '')}
            allowClear
          />
          <Select
            placeholder={t('operationLog.allActions')}
            style={{ width: 130 }}
            value={action}
            onChange={(v) => setAction(v)}
            allowClear
            loading={actionsLoading}
            options={actionOptions.map((o) => ({
              label: getOptionLabel(o, locale, t('operationLog.unknownAction')),
              value: o.action,
            }))}
            notFoundContent={
              actionsLoading ? t('common.loading') : actionsError ? t('common.loadFailed') : t('common.noData')
            }
          />
          <Select
            placeholder={t('operationLog.allResources')}
            style={{ width: 130 }}
            value={resource}
            onChange={(v) => setResource(v)}
            allowClear
            loading={resourcesLoading}
            options={resourceOptions.map((o) => ({
              label: locale.startsWith('zh') ? o.label_zh : o.label_en,
              value: o.resource,
            }))}
            notFoundContent={
              resourcesLoading ? t('common.loading') : resourcesError ? t('common.loadFailed') : t('common.noData')
            }
          />
          <RangePicker
            value={dateRange}
            onChange={(v) => setDateRange(v as [dayjs.Dayjs, dayjs.Dayjs] | null)}
            allowClear
            style={{ width: 240 }}
          />
          <Button onClick={() => load(1)}>{t('operationLog.refresh')}</Button>
        </Space>
        <Table
          columns={columns}
          dataSource={logs}
          rowKey="id"
          loading={loading}
          scroll={{ y: 'calc(100vh - 340px)' }}
          pagination={{
            current: page,
            total,
            pageSize,
            onChange: (p, ps) => {
              setPageSize(ps);
              load(p, ps);
            },
            showTotal: (total) => t('common.totalPage', { total }),
          }}
          size="small"
        />
      </div>

      <LogDetailModal
        open={!!detailItem}
        detailItem={detailItem}
        actionOptions={actionOptions}
        resourceOptions={resourceOptions}
        locale={locale}
        onClose={() => setDetailItem(null)}
      />
    </div>
  );
}
