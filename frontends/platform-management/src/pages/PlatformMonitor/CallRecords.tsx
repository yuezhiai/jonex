import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Form, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { CommonTable } from '@jonex/shared-lib';
import type { CommonTableRef, FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import { getCallRecords } from '../../api/platformMonitor';
import useMetaFilters from './useMetaFilters';
import {
  PM_ALL,
  PM_RECORD_FILTER_PARAMS,
  PM_RECORD_PAGE_SIZE,
  PM_RECORD_REQUEST_ID_WIDTH,
  PM_RECORD_SCROLL_X,
  PM_RECORD_TRACE_ID_WIDTH,
} from '../../constants/platformMonitor';
import { fmtLatencyOrDash } from './format';
import { enumLabel } from './labels';
import { capabilityLabel, expandKeywordTargets } from './capabilityLabels';
import { buildCallTargetOptions, buildSourceOptions } from './identifierOptions';
import IdentifierText from './components/IdentifierText';
import { CallStatusTag } from './components/Tags';
import ModuleError from './components/ModuleError';
import MetaHint from './components/MetaHint';
import TargetObjectFilter from './components/TargetObjectFilter';
import CallDetailDrawer from './components/CallDetailDrawer';
import type {
  PmCallDirection,
  PmCallProtocol,
  PmCallRecord,
  PmCallRecordFilters,
  PmCallStatus,
  PmPaneProps,
  PmWindow,
} from './types';

const DIRECTIONS: PmCallDirection[] = ['inbound', 'outbound'];
const STATUSES: PmCallStatus[] = ['success', 'failed', 'cancelled', 'running', 'unknown'];
const TRIGGERS: PmCallRecord['triggerType'][] = ['user', 'system'];
// 协议下拉候选：invoke / mcp / openai 均有写入方（mcp 来自 MCP 工具调用）；
// http 仍无写入方，不提供选项。
export const PROTOCOLS: PmCallProtocol[] = ['invoke', 'mcp', 'openai'];
// 下钻链接里的 protocol 仍按接口的全量枚举校验（含暂未出现在下拉里的 http）：
// 将来出现新的写入方时，记录页不会因为入口没列进下拉就把它当成脏链接丢掉。
const PROTOCOL_PARAM_VALUES: PmCallProtocol[] = ['invoke', 'mcp', 'http', 'openai'];

/** 读 URL 里的枚举筛选：不在候选里的值一律忽略，脏链接不该把列表筛成空 */
function readEnumFilter<T extends string>(
  params: URLSearchParams,
  name: string,
  allowed: readonly T[],
): T | undefined {
  const raw = params.get(name);
  return raw && (allowed as readonly string[]).includes(raw) ? (raw as T) : undefined;
}

/**
 * 调用记录列表（C6）。
 *
 * 筛选区 / 取数 / 表格 / 三态 / 分页整块交给 `CommonTable`（见其 README），本页只负责三件事：
 * 声明筛选项、把 `fetch` 的参数翻译成接口契约、给行加上点击开详情。所以**没有卡片外壳与标题**
 * —— 那一层是列表页组件自己组织的。
 *
 * 表格撑满页签内容区的剩余高度、只有表体滚（表头与分页固定）。antd 的固定表头只认具体像素，
 * 所以在这里量宿主容器（理由与写法同 `pages/DomainKnowledgeBlank/DocumentLibrary`）。
 */
export default function CallRecords({ window, refreshKey, manualRefreshKey }: PmPaneProps) {
  const { t } = useTranslation();
  const meta = useMetaFilters();
  const [searchParams, setSearchParams] = useSearchParams();
  const tableRef = useRef<CommonTableRef>(null);

  /**
   * 筛选初值取自 URL：KPI 卡下钻会带 `?status=&direction=`，对象详情「查看全部调用」
   * 会带 `?target=&protocol=`；刷新 / 分享链接也走同一条路。
   *
   * 只在挂载时读一次，之后筛选归表格自己管、并由 `syncFiltersToUrl` 单向写回；
   * 非法取值直接忽略，免得脏链接把列表筛成空。
   */
  const [initialFilters] = useState<Partial<PmCallRecordFilters>>(() => {
    const next: Partial<PmCallRecordFilters> = {};
    const direction = readEnumFilter(searchParams, PM_RECORD_FILTER_PARAMS.direction, DIRECTIONS);
    if (direction) next.direction = direction;
    const status = readEnumFilter(searchParams, PM_RECORD_FILTER_PARAMS.status, STATUSES);
    if (status) next.status = status;
    const triggerType = readEnumFilter(searchParams, PM_RECORD_FILTER_PARAMS.triggerType, TRIGGERS);
    if (triggerType) next.triggerType = triggerType;
    // 校验用接口的全量枚举（PROTOCOL_PARAM_VALUES）而不是下拉候选：
    // 将来出现 mcp / http 写入方时，记录页不会把入口给的链接当成脏链接丢掉
    const protocol = readEnumFilter(searchParams, PM_RECORD_FILTER_PARAMS.protocol, PROTOCOL_PARAM_VALUES);
    if (protocol) next.protocol = protocol;
    const source = searchParams.get(PM_RECORD_FILTER_PARAMS.source)?.trim();
    if (source) next.source = source;
    const target = searchParams.get(PM_RECORD_FILTER_PARAMS.target)?.trim();
    if (target) next.target = target;
    const keyword = searchParams.get(PM_RECORD_FILTER_PARAMS.keyword)?.trim();
    if (keyword) next.keyword = keyword;
    return next;
  });

  /**
   * 筛选写回 URL（单向：表格 → URL）。
   *
   * 空值删键而不是写空串，否则「已清空的筛选」会在链接里留下无意义的键。
   * `replace` 是必须的：每改一次筛选都压一条历史，浏览器后退键就再也退不出这一页。
   */
  const syncFiltersToUrl = useCallback(
    (params: TableQueryParams<PmCallRecordFilters>) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        (Object.keys(PM_RECORD_FILTER_PARAMS) as (keyof PmCallRecordFilters)[]).forEach((field) => {
          const value = params[field];
          const text = typeof value === 'string' ? value.trim() : '';
          if (text) next.set(PM_RECORD_FILTER_PARAMS[field], text);
          else next.delete(PM_RECORD_FILTER_PARAMS[field]);
        });
        return next;
      }, { replace: true });
    },
    [setSearchParams],
  );

  /** 保留原始异常：模块级错误提示要按 bizCode / HTTP 状态分流（3006 / 5001） */
  const [error, setError] = useState<unknown>(null);
  /** 详情抽屉的定位键。降级模式下同一 requestId 有多行，必须连 attemptId 一起带过去 */
  const [detailTarget, setDetailTarget] = useState<{ requestId: string; attemptId: string | null } | null>(null);

  // 表体高度。这个值只是 antd 的 `max-height` 上限，实际高度由 `.yx-pm-records-table` 那条
  // flex 链分配，所以取容器全高即可、不必自己扣表头与分页。取容器而不用 `calc(100vh - N)`：
  // hosted 模式下 100vh 与 standalone 不是一个基准，同一个 N 会一边多算一边不够。
  const hostRef = useRef<HTMLDivElement>(null);
  const [bodyHeight, setBodyHeight] = useState(400);
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const sync = () => setBodyHeight(Math.max(host.clientHeight, 200));
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const filters = useMemo<FilterConfig[]>(() => {
    // 下拉一律「占位符即『不过滤』」，不另设一个「全部」选项 —— 那个选项选完也是不过滤，
    // 同一个状态两种表示（undefined / 'all'）会让「重置」之后的回显跟选过的样子对不上。
    // CommonTable 的下拉自带 allowClear，清空即回到占位态。
    const select = (
      key: keyof PmCallRecordFilters,
      label: string,
      placeholder: string,
      values: string[],
      prefix: string,
    ): FilterConfig => ({
      key,
      label,
      type: 'select',
      placeholder,
      options: values.map((value) => ({ value, label: t(`${prefix}.${value}`) })),
    });

    const base: FilterConfig[] = [
      select(
        'direction',
        t('platformMonitor.records.column.direction'),
        t('platformMonitor.records.allDirections'),
        DIRECTIONS,
        'platformMonitor.callDirection',
      ),
      select(
        'status',
        t('platformMonitor.records.column.status'),
        t('platformMonitor.records.allStatuses'),
        STATUSES,
        'platformMonitor.callStatus',
      ),
      select(
        'triggerType',
        t('platformMonitor.records.column.triggerType'),
        t('platformMonitor.records.allTriggers'),
        TRIGGERS,
        'platformMonitor.callTrigger',
      ),
      select(
        'protocol',
        t('platformMonitor.call.column.protocol'),
        t('platformMonitor.records.allProtocols'),
        PROTOCOLS,
        'platformMonitor.callProtocol',
      ),
    ];

    // 「来源」候选只有元信息接口有；该接口同时返回 value/label 分离的 call_source_options，
    // 旧后端 call_sources 归一后仍可用；两者都没有就退成自由输入。
    const sourceOptions = buildSourceOptions(meta);
    const source: FilterConfig = sourceOptions.length
      ? {
          key: 'source',
          label: t('platformMonitor.records.filterSource'),
          type: 'select',
          placeholder: t('platformMonitor.records.sourcePlaceholder'),
          // value=原始 source（请求参数），label=账号名 / 认证类型（展示）
          options: sourceOptions,
        }
      : {
          key: 'source',
          label: t('platformMonitor.records.filterSource'),
          type: 'input',
          placeholder: t('platformMonitor.records.sourcePlaceholder'),
        };

    return [
      ...base,
      source,
      // 「目标对象」不再要求用户输入原始能力 ID —— 改用 filterExtra 里的对象选择器
      {
        key: 'keyword',
        label: t('platformMonitor.records.filterKeyword'),
        type: 'input',
        placeholder: t('platformMonitor.records.searchPlaceholder'),
      },
    ];
  }, [meta, t]);

  /**
   * 目标对象选择器候选：优先元信息接口的 `callTargets`，缺失时回退能力映射表。
   * 允许自由输入（模型名 / MCP 工具名本身就是可读名），提交的始终是原始标识。
   */
  const targetOptions = useMemo(() => buildCallTargetOptions(t, meta), [meta, t]);

  const fetchRecords = useCallback(
    async ({
      page,
      pageSize,
      direction,
      status,
      triggerType,
      protocol,
      source,
      target,
      keyword,
    }: TableQueryParams<PmCallRecordFilters>) => {
      const kw = keyword?.trim() ?? '';
      // 显示名（能力中英文释义 + 元信息接口的对外对象标识）展开为原始值，作为 target_any 下发；
      // 原 keyword 照常发送，两者并存（后端对 source 的用户名展开也继续生效）。
      const targetAny = expandKeywordTargets(kw, meta?.callTargets ?? [], t);

      const data = await getCallRecords(window, {
        // 筛选控件被清空时值是 undefined，而接口契约用 'all' / 空串表示「不筛」
        direction: direction ?? PM_ALL,
        status: status ?? PM_ALL,
        triggerType: triggerType ?? PM_ALL,
        protocol: protocol ?? PM_ALL,
        // 文本项去首尾空白：末尾多一个空格会让后端等值匹配一条都查不到，
        // 而用户看不出来自己敲了空格
        source: source?.trim() ?? '',
        target: target?.trim() ?? '',
        keyword: kw,
        targetAny,
        page,
        pageSize,
      });
      return { list: data.items, total: data.total };
    },
    [window, meta, t],
  );

  /**
   * 定时刷新的失败不回错误态：弹一整屏错误会把正在看的列表整块换掉，
   * 而下一秒的轮询可能就恢复了。真正的持续故障会由用户下一次手动刷新暴露。
   */
  const handleError = useCallback((e: unknown, info: { silent: boolean }) => {
    if (info.silent) return;
    setError(e);
  }, []);

  /**
   * 时间窗与刷新信号是**页面外部**的查询条件，进不了 `filters`，CommonTable 看不见它们变 ——
   * 必须显式 `refresh()` 一次；那一次读的是最新渲染的 `fetchRecords`，`window` 也就是新值。
   *
   * 三者姿态不同：换时间窗要回第一页（旧页码在新窗口里可能已不存在）、手动刷新停在当前页
   * 且要有可见的 loading、定时刷新既不翻页也不亮遮罩（每 60 秒闪一次等于页面一直在重载）。
   *
   * 首次挂载不补刀：组件自己已经取过第一份数。用「与上一次的值比较」而不是 mounted 标记。
   */
  const prevExtRef = useRef<{ window: PmWindow; refreshKey: number; manualRefreshKey: number }>({
    window,
    refreshKey,
    manualRefreshKey,
  });
  useEffect(() => {
    const prev = prevExtRef.current;
    prevExtRef.current = { window, refreshKey, manualRefreshKey };
    if (prev.window !== window) {
      tableRef.current?.refresh({ resetPage: true });
      return;
    }
    if (prev.manualRefreshKey !== manualRefreshKey) {
      tableRef.current?.refresh();
      return;
    }
    if (prev.refreshKey !== refreshKey) tableRef.current?.refresh({ silent: true });
  }, [window, refreshKey, manualRefreshKey]);

  const columns = useMemo<TableColumnsType<PmCallRecord>>(
    () => [
      { title: t('platformMonitor.records.column.time'), dataIndex: 'time', width: 150 },
      {
        title: t('platformMonitor.records.column.direction'),
        dataIndex: 'direction',
        width: 140,
        render: (value: PmCallDirection) => enumLabel(t, 'platformMonitor.callDirection', value),
      },
      {
        title: t('platformMonitor.records.column.triggerType'),
        dataIndex: 'triggerType',
        width: 110,
        render: (value: PmCallRecord['triggerType']) => enumLabel(t, 'platformMonitor.callTrigger', value),
      },
      {
        title: t('platformMonitor.records.column.caller'),
        dataIndex: 'caller',
        width: 150,
        // 优先后端解析出的账号名；解析不到时回退原始 source（认证类型 / platform 等）
        render: (value: string | null, row: PmCallRecord) => row.callerName ?? value ?? '-',
      },
      {
        title: t('platformMonitor.records.column.target'),
        dataIndex: 'target',
        // 能力 ID 显示释义，模型名 / MCP 工具名原样；原始值只在 hover 与复制入口
        render: (value: string) => <IdentifierText value={value} label={capabilityLabel(t, value)} strong />,
      },
      {
        title: t('platformMonitor.records.column.method'),
        dataIndex: 'method',
        width: 110,
        render: (value: PmCallProtocol | null) => enumLabel(t, 'platformMonitor.callProtocol', value),
      },
      {
        title: t('platformMonitor.records.column.status'),
        dataIndex: 'status',
        width: 100,
        render: (value: PmCallStatus) => <CallStatusTag status={value} />,
      },
      {
        title: t('platformMonitor.records.column.latency'),
        dataIndex: 'latencyMs',
        align: 'right',
        width: 110,
        render: fmtLatencyOrDash,
      },
      {
        title: t('platformMonitor.records.column.errorCode'),
        dataIndex: 'errorCode',
        width: 110,
        // 成功没有错误码，给占位符而不是留空 —— 空单元格读起来像「没查到」
        render: (value: string | null) => value ?? '-',
      },
      {
        title: t('platformMonitor.records.column.requestId'),
        dataIndex: 'requestId',
        // 宽度按最长的一类 ID 给：`rt-terminal:<uuid>` 是 48 字符，光靠定宽跟不上，
        // 所以同时上 `ellipsis` —— 内容不许溢出单元格。这不是美观问题：antd 的固定表头
        // 是「表头表体两张独立表格 + 同步滚动」，一旦某一格的内容撑出表格宽度，表体的
        // 可滚距离就会大于表头，两边行程对不上，横滚时列必然错位。
        width: PM_RECORD_REQUEST_ID_WIDTH,
        render: (value: string) => (
          <Typography.Text className="yx-pm-request-id" copyable={{ text: value }} ellipsis={{ tooltip: value }}>
            {value}
          </Typography.Text>
        ),
      },
      {
        // 链路关联 ID：排查跨服务问题时，用户要拿它去别的系统里搜，所以给复制按钮
        title: t('platformMonitor.records.column.traceId'),
        dataIndex: 'traceId',
        width: PM_RECORD_TRACE_ID_WIDTH,
        // 老数据与未接通采集链路的调用没有 trace_id，落占位符（否则空单元格像没查到）
        render: (value: string | null) =>
          value ? (
            <Typography.Text className="yx-pm-request-id" copyable={{ text: value }} ellipsis={{ tooltip: value }}>
              {value}
            </Typography.Text>
          ) : (
            '-'
          ),
      },
    ],
    [t],
  );

  if (error) {
    return (
      // 重试即把 CommonTable 重新挂载 —— 挂载本身会取一次数，不必再手动触发
      <ModuleError error={error} onRetry={() => setError(null)} />
    );
  }

  return (
    <div className="yx-pm-pane yx-pm-pane--fill">
      {/* 「来源」下拉依赖 M1；拿不到时退成自由输入，这里补一句「候选可能不全」 */}
      <MetaHint />

      <div className="yx-pm-records-host" ref={hostRef}>
        <CommonTable<PmCallRecord, PmCallRecordFilters>
          ref={tableRef}
          className="yx-pm-records-table"
          filters={filters}
          initialFilters={initialFilters}
          /**
           * 页面顶部的「刷新」已经覆盖本表（manualRefreshKey → tableRef.refresh()），
           * 还额外重置自动刷新计时、更新「统计截至」；表格自带的刷新会是第二个重复入口。
           * 「重置」保留 —— 它只清本表筛选，页面级按钮没有这个职责。
           */
          actions={{ refresh: false }}
          /**
           * 对象选择器：配置项的 select 不支持自由输入，用 filterExtra 挂 AutoComplete。
           * 输入框回显选项 label（如「文档解析」），下发给接口的 value 仍是原始标识；
           * 打开下拉即列出全部候选（不用先清空），用户也可以直接输入模型名 / MCP 工具名。
           * 「全部对象」用空值表示，与「清空筛选 = 不筛」的既有语义一致。
           */
          filterExtra={(ctx) => (
            <Form.Item
              label={t('platformMonitor.records.filterTarget')}
              labelCol={{ flex: '0 0 5.5em', style: { textAlign: 'right' } }}
              wrapperCol={{ flex: '1 1 0%', style: { minWidth: 0 } }}
              style={{ marginBottom: 0 }}
            >
              <TargetObjectFilter
                value={(ctx.get('target') as string | undefined) ?? ''}
                options={targetOptions}
                placeholder={t('platformMonitor.records.allObjects')}
                onChange={(value) => ctx.set('target', value as PmCallRecordFilters['target'])}
              />
            </Form.Item>
          )}
          columns={columns}
          // 降级模式下 requestId 会重复，只有 attemptId 唯一；正常模式（attemptId 为 null）回落
          // requestId + direction —— 同一次能力派发可能同时落 inbound/outbound 两条 N1，
          // 只认 requestId 会在这种情况下撞 key
          rowKey={(row) => `${row.requestId}:${row.attemptId ?? row.direction}`}
          size="small"
          scroll={{ x: PM_RECORD_SCROLL_X, y: bodyHeight }}
          defaultPageSize={PM_RECORD_PAGE_SIZE}
          rowClassName={() => 'yx-pm-row-clickable'}
          onRow={(row) => ({
            onClick: () => setDetailTarget({ requestId: row.requestId, attemptId: row.attemptId }),
          })}
          fetch={fetchRecords}
          onError={handleError}
          onQueryChange={syncFiltersToUrl}
        />
      </div>

      <CallDetailDrawer
        open={detailTarget !== null}
        requestId={detailTarget?.requestId ?? null}
        attemptId={detailTarget?.attemptId ?? null}
        onClose={() => setDetailTarget(null)}
      />
    </div>
  );
}
