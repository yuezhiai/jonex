import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Result, Tooltip } from 'antd';
import type { TableColumnsType } from 'antd';
import { EyeOutlined, ReloadOutlined } from '@ant-design/icons';
import { CommonTable, formatDateTime } from '@jonex/shared-lib';
import type { FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import { listMcpServices } from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import type { McpServiceItem } from '@/api/mcpServices';
import McpServiceDetailDrawer, { type McpServiceDetailDrawerHandle } from './McpServiceDetailDrawer';
import './index.css';

interface McpServicesTabProps {
  /** 点击「前往 MCP Key 管理」时切换到服务访问Key 视图 */
  onGoToKeys?: () => void;
}

/** 必须是 type 别名而不是 interface：CommonTable 的 F 要求隐式索引签名 */
type ServiceFilters = {
  keyword?: string;
  space_id?: string;
};

/** 资源范围标签最多直出几个，其余折叠成 +N */
const SCOPE_TAG_LIMIT = 3;

/** MCP 服务目录 — 服务列表 Tab */
export default function McpServicesTab({ onGoToKeys }: McpServicesTabProps) {
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  /** 接口 HTTP status（403=权限不足时错误态换用权限图标，而非报错图标） */
  const [errorStatus, setErrorStatus] = useState<number | undefined>(undefined);
  /** 接口返回的全量条数：区分「本来就没有服务」与「筛选没命中」两种空态 */
  const [totalCount, setTotalCount] = useState<number | null>(null);
  // 领域空间筛选选项（来源 GET /knowledge-base/spaces 接口，而非列表数据聚合）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);

  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);

  /**
   * 取数 / 过滤 / 切片全在这里 —— CommonTable 只认这一个数据出口。
   * ⚠️ 关键词与空间都从参数取，不要读页面 state：fetch 存在 ref 里，闭包不参与重渲染。
   */
  const fetchRows = useCallback(
    async ({ page, pageSize, keyword, space_id }: TableQueryParams<ServiceFilters>) => {
      const q = (keyword ?? '').trim().toLocaleLowerCase();
      try {
        const result = await listMcpServices();
        const all = result.items || [];
        setTotalCount(all.length);
        setError(null);
        setErrorStatus(undefined);
        const rows = all.filter((item) => {
          if (
            q &&
            !`${item.name || ''} ${item.tool || ''} ${item.space_name || ''}`.toLocaleLowerCase().includes(q)
          ) {
            return false;
          }
          if (space_id && item.space_id !== space_id) return false;
          return true;
        });
        const start = (page - 1) * pageSize;
        return { list: rows.slice(start, start + pageSize), total: rows.length };
      } catch (err: unknown) {
        // 接口报错：由下面的 Result 分支渲染错误态 + 重试按钮
        setError(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
        setErrorStatus(
          typeof (err as { status?: number } | null)?.status === 'number'
            ? (err as { status?: number }).status
            : undefined,
        );
        throw err;
      }
    },
    [t],
  );

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('common.keyword'),
        type: 'input',
        placeholder: t('mcpServiceDirectory.searchServicePlaceholder'),
      },
      {
        key: 'space_id',
        label: t('mcpServiceDirectory.colSpace'),
        type: 'select',
        placeholder: t('mcpServiceDirectory.allSpaces'),
        options: spaceOptions,
      },
    ],
    [t, spaceOptions],
  );

  /** 领域空间筛选选项（来源接口 GET /knowledge-base/spaces，失败静默为空） */
  useEffect(() => {
    listSpaces()
      .then((list) => setSpaceOptions(list.map((s) => ({ label: s.name ?? s.id, value: s.id }))))
      .catch(() => {
        // 失败静默，下拉仅剩「全部领域空间」
      });
  }, []);

  /** 发布状态 + 是否可调用，压成一条带圆点的状态文案 */
  const renderStatus = (record: McpServiceItem) => {
    if (!record.is_published) {
      return <span className="mcp-status is-unpublished">{t('mcpServiceDirectory.statusUnpublished')}</span>;
    }
    if (record.enabled === 0) {
      return (
        <Tooltip title={t('mcpServiceDirectory.statusUnavailableTip')}>
          <span className="mcp-status is-off">{t('mcpServiceDirectory.statusUnavailable')}</span>
        </Tooltip>
      );
    }
    return <span className="mcp-status is-active">{t('mcpServiceDirectory.statusPublished')}</span>;
  };

  /** 默认资源范围：系统内置服务由 Key 授权决定，领域服务列出 KB（超过 3 个折叠） */
  const renderScope = (record: McpServiceItem) => {
    if (record.service_type === 'system') {
      return <span className="mcp-cell-muted">{t('mcpServiceDirectory.scopeByKey')}</span>;
    }
    const names = record.kb_names || [];
    if (names.length === 0) return <span className="mcp-cell-muted">-</span>;
    const shown = names.slice(0, SCOPE_TAG_LIMIT);
    const rest = names.slice(SCOPE_TAG_LIMIT);
    return (
      <div className="mcp-chip-row">
        {shown.map((n) => (
          <span className="mcp-chip" key={n}>
            {n}
          </span>
        ))}
        {rest.length > 0 && (
          <Tooltip title={rest.join('、')}>
            <span className="mcp-chip is-more">+{rest.length}</span>
          </Tooltip>
        )}
      </div>
    );
  };

  const columns: TableColumnsType<McpServiceItem> = [
    {
      title: t('mcpServiceDirectory.colName'),
      dataIndex: 'name',
      key: 'name',
      width: 200,
      render: (name: string, record: McpServiceItem) => (
        <Button
          type="text"
          className="mcp-cell-name"
          title={name}
          onClick={() => detailDrawerRef.current?.open(record)}
        >
          {name}
        </Button>
      ),
    },
    {
      title: t('mcpServiceDirectory.colCapabilityType'),
      key: 'capability_type',
      width: 130,
      render: (_: unknown, record: McpServiceItem) => (
        <span className={`mcp-chip ${record.service_type === 'system' ? 'is-call' : ''}`}>
          {record.service_type === 'system'
            ? t('mcpServiceDirectory.capabilityWrite')
            : t('mcpServiceDirectory.capabilityDomain')}
        </span>
      ),
    },
    {
      title: t('mcpServiceDirectory.colToolName'),
      key: 'tool_name',
      width: 170,
      render: (_: unknown, record: McpServiceItem) =>
        record.tool ? (
          <span className="mcp-cell-mono" title={record.tool}>
            {record.tool}
          </span>
        ) : (
          <span className="mcp-cell-muted">-</span>
        ),
    },
    {
      title: t('mcpServiceDirectory.colSource'),
      key: 'source',
      width: 150,
      render: (_: unknown, record: McpServiceItem) =>
        record.service_type === 'system' ? (
          <span className="mcp-cell-text">{t('mcpServiceDirectory.sourceBuiltin')}</span>
        ) : record.space_name ? (
          <span className="mcp-cell-text" title={record.space_name}>
            {record.space_name}
          </span>
        ) : (
          <span className="mcp-cell-muted">-</span>
        ),
    },
    {
      title: t('mcpServiceDirectory.colResourceScope'),
      key: 'resource_scope',
      width: 300,
      render: (_: unknown, record: McpServiceItem) => renderScope(record),
    },
    {
      title: t('mcpServiceDirectory.colStatus'),
      key: 'is_published',
      width: 140,
      render: (_: unknown, record: McpServiceItem) => renderStatus(record),
    },
    {
      title: t('mcpServiceDirectory.colLastCall'),
      dataIndex: 'last_call_at',
      key: 'last_call_at',
      width: 150,
      render: (v: string | null) =>
        v ? (
          <span className="mcp-cell-date">{formatDateTime(v)}</span>
        ) : (
          <span className="mcp-cell-muted">{t('mcpServiceDirectory.neverCalled')}</span>
        ),
    },
    {
      title: t('mcpServiceDirectory.colActions'),
      key: 'actions',
      width: 84,
      render: (_: unknown, record: McpServiceItem) => (
        <Tooltip title={t('mcpServiceDirectory.viewDetail')}>
          <Button
            type="text"
            className="mcp-icon-btn"
            icon={<EyeOutlined />}
            onClick={() => detailDrawerRef.current?.open(record)}
          />
        </Tooltip>
      ),
    },
  ];

  // ── 错误态覆盖：接口失败时渲染 Result + 重试按钮 ──
  // 权限不足(403)时使用 antd 内置 403 权限图标，而非报错图标
  if (error) {
    const forbidden = errorStatus === 403;
    return (
      <Result
        status={forbidden ? '403' : 'error'}
        title={forbidden ? t('mcpServiceDirectory.forbiddenTitle') : t('mcpServiceDirectory.loadFailed')}
        subTitle={error}
        extra={
          // ⚠️ 这里不能调 tableRef.current.refresh()：错误态下 CommonTable 已被卸载、ref 为 null。
          // 清掉 error 让它重新挂载，挂载时取数 effect 会自己跑一次。
          <Button type="primary" icon={<ReloadOutlined />} onClick={() => setError(null)}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="mcp-tab-body">
      <CommonTable<McpServiceItem, ServiceFilters>
        rowKey="id"
        filters={filters}
        columns={columns}
        fetch={fetchRows}
        size="middle"
        scroll={{ x: 1330 }}
        emptyText={
          totalCount === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.emptyText')} />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.emptySearchText')} />
          )
        }
      />

      <McpServiceDetailDrawer ref={detailDrawerRef} onGoToKeys={onGoToKeys} />
    </div>
  );
}
