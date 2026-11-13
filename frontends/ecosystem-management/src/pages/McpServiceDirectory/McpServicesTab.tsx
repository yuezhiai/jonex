import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Modal, Result, Tooltip, message } from 'antd';
import type { TableColumnsType } from 'antd';
import { EyeOutlined, ReloadOutlined } from '@ant-design/icons';
import { CommonTable, formatDateTime, usePermission } from '@jonex/shared-lib';
import type { CommonTableRef, FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import {
  listMcpServices,
  publishService,
  startService,
  stopService,
  unpublishService,
} from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import type { McpServiceItem } from '@/api/mcpServices';
import McpServiceDetailDrawer, { type McpServiceDetailDrawerHandle } from './McpServiceDetailDrawer';
import { useFilledTableHeight } from './useFilledTableHeight';
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

/** 发布态动作：与后端 status 一一对应（unpublished → publish；published → stop；stopped → start） */
type ServiceAction = 'publish' | 'unpublish' | 'stop' | 'start';

/** MCP 服务目录 — 服务列表 Tab */
export default function McpServicesTab({ onGoToKeys }: McpServicesTabProps) {
  const { t } = useTranslation();
  // 权限（shell 下发）：与 Key 管理页同一套 usePermission，管理动作要求 mcp:service:manage
  const { permissions: userPermissions } = usePermission();
  const canManage = userPermissions.includes('mcp:service:manage');
  const tableRef = useRef<CommonTableRef>(null);
  /** 正在执行状态变更的服务 ID：用于按钮级 loading，避免重复提交 */
  const [actingId, setActingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** 接口 HTTP status（403=权限不足时错误态换用权限图标，而非报错图标） */
  const [errorStatus, setErrorStatus] = useState<number | undefined>(undefined);
  /** 接口返回的全量条数：区分「本来就没有服务」与「筛选没命中」两种空态 */
  const [totalCount, setTotalCount] = useState<number | null>(null);
  // 领域空间筛选选项（来源 GET /knowledge-base/spaces 接口，而非列表数据聚合）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);

  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);
  /** 表体宿主：高度固定，20 条/页时只有表体滚动（表头、筛选区、分页都钉住） */
  const [tableHostRef, tableBodyHeight] = useFilledTableHeight();

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
            !`${item.name || ''} ${item.tool || ''} ${item.tool_description || ''} ${item.space_name || ''}`
              .toLocaleLowerCase()
              .includes(q)
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

  /** 规范化发布态：以后端 status 为准，缺省时按 is_published 回退（兼容旧数据） */
  const publishStatusOf = (record: McpServiceItem): 'published' | 'unpublished' | 'stopped' => {
    if (record.status === 'stopped') return 'stopped';
    if (record.status === 'unpublished' || !record.is_published) return 'unpublished';
    return 'published';
  };

  /** 发布状态 + 是否可调用，压成一条带圆点的状态文案 */
  const renderStatus = (record: McpServiceItem) => {
    const status = publishStatusOf(record);
    if (status === 'unpublished') {
      return <span className="mcp-status is-unpublished">{t('mcpServiceDirectory.statusUnpublished')}</span>;
    }
    if (status === 'stopped') {
      return (
        <Tooltip title={t('mcpServiceDirectory.statusStoppedTip')}>
          <span className="mcp-status is-revoked">{t('mcpServiceDirectory.statusStopped')}</span>
        </Tooltip>
      );
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

  /** 执行发布态变更（成功后统一刷新列表；409 按场景给出可读原因） */
  const runAction = useCallback(
    async (record: McpServiceItem, action: ServiceAction) => {
      const map: Record<ServiceAction, (id: string) => Promise<unknown>> = {
        publish: publishService,
        unpublish: unpublishService,
        stop: stopService,
        start: startService,
      };
      setActingId(record.id);
      try {
        await map[action](record.id);
        message.success(t(`mcpServiceDirectory.${action}Success`));
        tableRef.current?.refresh();
      } catch (err: unknown) {
        const status = (err as { status?: number } | null)?.status;
        // 发布前必须已保存 Tool：后端 409；这里给专门的清晰提示，不落回通用报错
        if (action === 'publish' && status === 409) {
          message.error(t('mcpServiceDirectory.publishNeedTool'));
        } else {
          message.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
        }
      } finally {
        setActingId(null);
      }
    },
    [t],
  );

  /** 停用：先确认（明确告知已授权 Key 会失效），再执行 */
  const confirmStop = useCallback(
    (record: McpServiceItem) => {
      Modal.confirm({
        title: t('mcpServiceDirectory.confirmStopTitle'),
        content: t('mcpServiceDirectory.confirmStopContent', { name: record.name }),
        okText: t('mcpServiceDirectory.actionStop'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        onOk: () => runAction(record, 'stop'),
      });
    },
    [runAction, t],
  );

  /** 取消发布：先确认（服务从 MCP 目录可见范围移除，已授权 Key 无法调用） */
  const confirmUnpublish = useCallback(
    (record: McpServiceItem) => {
      Modal.confirm({
        title: t('mcpServiceDirectory.confirmUnpublishTitle'),
        content: t('mcpServiceDirectory.confirmUnpublishContent', { name: record.name }),
        okText: t('mcpServiceDirectory.actionUnpublish'),
        cancelText: t('common.cancel'),
        onOk: () => runAction(record, 'unpublish'),
      });
    },
    [runAction, t],
  );

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

  /**
   * 行内操作：查看图标常显；状态动作按后端规则出按钮，且仅领域服务 + 有管理权限时可见。
   * - unpublished：发布（未存 Tool 时禁用并提示）
   * - published：停用（tooltip 与确认弹窗都提示已授权 Key 将失效）、取消发布
   * - stopped：仅「启用」。不提供「取消发布」——取消发布会把 stopped_at 一并清空，
   *   后端可用性判定随即放行，等于把已停用服务解封；要下线必须先启用再取消发布。
   * - 系统写服务：不提供任何编辑/状态入口
   */
  const renderActions = (record: McpServiceItem) => {
    const status = publishStatusOf(record);
    // 系统写服务（固定合成 ID）不提供任何编辑 / 状态操作入口
    const isSystem = record.service_type === 'system' || record.id === 'system.knowledge_document_write';
    const busy = actingId === record.id;
    const publishDisabled = !record.tool;

    return (
      <div className="mcp-row-acts">
        <Tooltip title={t('mcpServiceDirectory.viewDetail')}>
          <Button
            type="text"
            className="mcp-icon-btn"
            icon={<EyeOutlined />}
            onClick={() => detailDrawerRef.current?.open(record)}
          />
        </Tooltip>

        {!isSystem && canManage && status === 'unpublished' && (
          <Tooltip title={publishDisabled ? t('mcpServiceDirectory.publishNeedTool') : undefined}>
            {/* 禁用按钮不触发鼠标事件，包一层 span 让 Tooltip 仍可显示原因 */}
            <span style={{ display: 'inline-block' }}>
              <Button
                type="link"
                size="small"
                className="mcp-act-link"
                disabled={publishDisabled}
                loading={busy}
                onClick={() => runAction(record, 'publish')}
              >
                {t('mcpServiceDirectory.actionPublish')}
              </Button>
            </span>
          </Tooltip>
        )}

        {!isSystem && canManage && status === 'published' && (
          <>
            <Tooltip title={t('mcpServiceDirectory.stopWarning')}>
              <Button
                type="link"
                size="small"
                danger
                className="mcp-act-link"
                loading={busy}
                onClick={() => confirmStop(record)}
              >
                {t('mcpServiceDirectory.actionStop')}
              </Button>
            </Tooltip>
            <Button
              type="link"
              size="small"
              className="mcp-act-link"
              disabled={busy}
              onClick={() => confirmUnpublish(record)}
            >
              {t('mcpServiceDirectory.actionUnpublish')}
            </Button>
          </>
        )}

        {!isSystem && canManage && status === 'stopped' && (
          <Button
            type="link"
            size="small"
            className="mcp-act-link"
            loading={busy}
            onClick={() => runAction(record, 'start')}
          >
            {t('mcpServiceDirectory.actionStart')}
          </Button>
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
      // 列宽固定 200：按钮内套一层 span 才能做单行省略，完整名称走 Tooltip
      render: (name: string, record: McpServiceItem) => (
        <Tooltip
          title={name}
          mouseEnterDelay={0.3}
          // 放宽默认 250px 上限并允许长英文串折行，悬停气泡不溢出
          styles={{ root: { maxWidth: 360 }, container: { wordBreak: 'break-all' } }}
        >
          <Button type="text" className="mcp-cell-name" onClick={() => detailDrawerRef.current?.open(record)}>
            <span className="mcp-cell-name-text">{name || '-'}</span>
          </Button>
        </Tooltip>
      ),
    },
    {
      title: t('mcpServiceDirectory.colStatus'),
      key: 'is_published',
      width: 140,
      render: (_: unknown, record: McpServiceItem) => renderStatus(record),
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
      title: t('mcpServiceDirectory.colToolDesc'),
      key: 'tool_description',
      width: 240,
      render: (_: unknown, record: McpServiceItem) =>
        record.tool_description ? (
          // 单行省略（.mcp-cell-text 自带 ellipsis）：完整描述走 Tooltip，不撑高行
          <Tooltip
            title={record.tool_description}
            styles={{ root: { maxWidth: 360 }, container: { wordBreak: 'break-all' } }}
          >
            <span className="mcp-cell-text">{record.tool_description}</span>
          </Tooltip>
        ) : (
          <span className="mcp-cell-muted">{t('mcpServiceDirectory.noDescription')}</span>
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
      width: 210,
      // 操作列固定在右侧：列表横向滚动时按钮始终可见（需配合 scroll.x）
      fixed: 'right',
      render: (_: unknown, record: McpServiceItem) => renderActions(record),
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
    <div className="mcp-tab-body" ref={tableHostRef}>
      <CommonTable<McpServiceItem, ServiceFilters>
        ref={tableRef}
        // .yx-table-fill 由 platform-theme 提供：表头/分页固定、表体 flex:1 滚动
        className="yx-table-fill"
        rowKey="id"
        filters={filters}
        columns={columns}
        fetch={fetchRows}
        size="middle"
        scroll={{ x: 1450, y: tableBodyHeight }}
        emptyText={
          totalCount === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.emptyText')} />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.emptySearchText')} />
          )
        }
      />

      <McpServiceDetailDrawer
        ref={detailDrawerRef}
        onGoToKeys={onGoToKeys}
        // Tool 保存后同步刷新列表（发布按钮是否可用取决于 tool 是否已保存）
        onSaved={() => tableRef.current?.refresh()}
      />
    </div>
  );
}
