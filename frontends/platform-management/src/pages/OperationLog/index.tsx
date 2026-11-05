import { useCallback, useEffect, useMemo, useState } from 'react';
import { DatePicker, Form, Tag, message } from 'antd';
import type { TableColumnsType } from 'antd';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import { CommonTable, PageContent, PageHeader } from '@jonex/shared-lib';
import type { FilterConfig, FilterContext, TableQueryParams } from '@jonex/shared-lib';
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

/**
 * 本页的查询条件。
 *
 * `range` 不是接口字段（接口要的是 `start_time` / `end_time` 两个 ISO 串），但仍必须是
 * 筛选值的一部分：CommonTable 的「重置」只清它视野里的筛选状态，区间若存在页面自己的
 * state 里，点完重置控件还留着旧值。翻译成接口参数的事放在 `fetch` 里做。
 */
type LogFilters = {
  keyword?: string;
  action?: string;
  range?: [dayjs.Dayjs, dayjs.Dayjs] | null;
};

/**
 * 筛选区的 label 定宽、控件吃满其余空间。取值照抄 CommonTable 内部那对常量 ——
 * shared-lib 没导出它们，不照抄的话 `range` 的 label 会按内容自适应，与同一行 keyword 对齐不上。
 */
const FILTER_LABEL_COL = { flex: '0 0 5.5em', style: { textAlign: 'right' as const } };
const FILTER_WRAPPER_COL = { flex: '1 1 0%', style: { minWidth: 0 } };

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

/**
 * 日志管理。
 *
 * 筛选区 / 取数 / 表格 / 三态 / 分页整块交给 `CommonTable`（见其 README），本页只负责
 * 三件事：声明筛选项、把筛选值翻译成接口契约、把行上的「详情」接上弹窗。
 */
export default function OperationLog() {
  const { t, i18n } = useTranslation();
  const locale = i18n.language || 'zh-CN';
  const [detailItem, setDetailItem] = useState<AuditLogItem | null>(null);

  // ---- 操作类型下拉动态数据 ----
  const [actionOptions, setActionOptions] = useState<AuditActionOption[]>([]);

  // ---- 资源类型映射 ----
  // 资源筛选已下线，但表格「资源」列与详情弹窗仍用它把码值翻成可读名；
  // 取不到时 getResourceLabel 会退回 resource_name / resource_id，故失败不提示
  const [resourceOptions, setResourceOptions] = useState<AuditResourceType[]>([]);

  useEffect(() => {
    let cancelled = false;
    listAuditActions()
      .then((opts) => {
        if (!cancelled) {
          const deduped = Array.from(new Map(opts.map((o) => [o.action, o])).values());
          setActionOptions(deduped);
        }
      })
      .catch(() => {
        /* 下拉拿不到就退化成「只用关键词筛」，列表本身照常可用 */
      });
    listAuditResourceTypes()
      .then((opts) => {
        if (!cancelled) setResourceOptions(opts);
      })
      .catch(() => {
        /* 映射不可用：getResourceLabel 退回 resource_name / resource_id */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const showDetail = useCallback(async (id: number) => {
    try {
      const detail = await getAuditLog(id);
      setDetailItem(detail);
    } catch {
      /* 详情拉不到不弹错误：列表行上的信息已足够，失败时保持原样即可 */
    }
  }, []);

  const columns = useMemo<TableColumnsType<AuditLogItem>>(
    () => [
      {
        title: t('operationLog.time'),
        dataIndex: 'created_at',
        key: 'created_at',
        width: 170,
        render: (v: string | null) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '--'),
      },
      {
        title: t('operationLog.user'),
        dataIndex: 'username',
        key: 'username',
        width: 90,
        render: (v: string | null) => v || '--',
      },
      {
        title: t('operationLog.action'),
        dataIndex: 'action',
        key: 'action',
        width: 90,
        render: (v: string) => (
          <Tag color={actionTagColor(v)}>
            {getActionLabel(locale, actionOptions, v, t('operationLog.unknownAction'))}
          </Tag>
        ),
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
      {
        title: t('operationLog.ip'),
        dataIndex: 'ip',
        key: 'ip',
        width: 120,
        render: (v: string | null) => v || '--',
      },
      {
        title: t('operationLog.duration'),
        dataIndex: 'duration_ms',
        key: 'duration_ms',
        width: 70,
        render: (v: number | null) => formatDuration(v),
      },
      {
        title: t('operationLog.outcome'),
        dataIndex: 'outcome',
        key: 'outcome',
        width: 70,
        render: (v: string | null | undefined) => {
          // 只认 outcome：业务失败 status_code=None（HTTP 200），不可用状态码推导
          if (v === 'FAILED') return <Tag color="red">{t('operationLog.failed')}</Tag>;
          if (v === 'SUCCESS') return <Tag color="green">{t('operationLog.success')}</Tag>;
          return <span style={{ color: '#94a3b8' }}>-</span>;
        },
      },
      {
        title: t('common.actions'),
        key: 'detail',
        width: 60,
        render: (_: unknown, r: AuditLogItem) => (
          <a className="yx-table-action" onClick={() => void showDetail(r.id)}>
            {t('operationLog.detail')}
          </a>
        ),
      },
    ],
    [t, locale, actionOptions, resourceOptions, showDetail],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('operationLog.keyword'),
        type: 'input',
        placeholder: t('operationLog.searchLog'),
      },
      {
        key: 'action',
        label: t('operationLog.action'),
        type: 'select',
        placeholder: t('operationLog.allActions'),
        // 码 → 文案复用详情那一份：两处各写一套，加了新 action 迟早只补一处
        options: actionOptions.map((o) => ({
          label: getOptionLabel(o, locale, t('operationLog.unknownAction')),
          value: o.action,
        })),
      },
    ],
    [t, locale, actionOptions],
  );

  /**
   * 时间区间。`RangePicker` 是配置项表达不了的控件，故走 filterExtra；
   * 值写进 FilterContext 而不是页面 state，重置时才会跟着一起清掉。
   */
  const renderRange = useCallback(
    (ctx: FilterContext<LogFilters>) => (
      <Form.Item
        label={t('operationLog.time')}
        labelCol={FILTER_LABEL_COL}
        wrapperCol={FILTER_WRAPPER_COL}
        style={{ marginBottom: 0 }}
      >
        <RangePicker
          value={ctx.get('range') ?? null}
          onChange={(v) => ctx.set('range', (v as [dayjs.Dayjs, dayjs.Dayjs] | null) ?? null)}
          allowClear
          style={{ width: '100%' }}
        />
      </Form.Item>
    ),
    [t],
  );

  /** 筛选值 → 接口参数。区间取「当天 00:00:00」到「当天 23:59:59.999」，与任务中心同口径。 */
  const fetchLogs = useCallback(async (params: TableQueryParams<LogFilters>) => {
    const [start, end] = params.range ?? [];
    const res = await listAuditLogs({
      page: params.page,
      page_size: params.pageSize,
      action: params.action,
      keyword: params.keyword,
      start_time: start?.startOf('day').toISOString(),
      end_time: end?.endOf('day').toISOString(),
    });
    return { list: res.items ?? [], total: res.total ?? 0 };
  }, []);

  /**
   * 取数失败的报警。CommonTable 只清空列表、不再自己渲染「加载失败」兜底，表格会照常
   * 显示成空态 —— 少了这条提示，「接口挂了」与「确实没有日志」在页面上就分不出来。
   */
  const handleListError = useCallback(
    (e: unknown) => {
      message.error(e instanceof Error && e.message ? e.message : t('common.loadFailed'));
    },
    [t],
  );

  return (
    <>
      <PageHeader title={t('operationLog.title')} />
      {/* 内容撑开：一页 10 行，不需要在卡片里再套一层滚动条，页面整体跟着 .yx-content 滚 */}
      <PageContent height="content">
        <CommonTable<AuditLogItem, LogFilters>
          filters={filters}
          filterExtra={renderRange}
          columns={columns}
          rowKey="id"
          size="small"
          fetch={fetchLogs}
          onError={handleListError}
        />
      </PageContent>
      <LogDetailModal
        open={Boolean(detailItem)}
        detailItem={detailItem}
        actionOptions={actionOptions}
        resourceOptions={resourceOptions}
        locale={locale}
        onClose={() => setDetailItem(null)}
      />
    </>
  );
}
