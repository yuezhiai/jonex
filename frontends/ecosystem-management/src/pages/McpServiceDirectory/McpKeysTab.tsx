import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Typography, Empty, Button, Result, Modal, Tooltip, Dropdown, message } from 'antd';
import type { MenuProps } from 'antd';
import { EllipsisOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { listMcpKeys, revokeMcpKey, toggleMcpKey, deleteMcpKey, type McpKeyItem } from '@/api/mcpKeys';
import type { WriteGrant } from '@/api/mcpKeys';
import { listKnowledgeBases, type KnowledgeBaseBrief } from '@/api/spaces';
import { PermButton, usePermission, CommonTable } from '@jonex/shared-lib';
import type { CommonTableRef, FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import { McpKeyDrawer, type McpKeyDrawerHandle } from './McpKeyDrawer';
import { RecreateKeyModal, type RecreateKeyModalHandle } from './RecreateKeyModal';
import McpKeyDetailDrawer, { type McpKeyDetailDrawerHandle } from './McpKeyDetailDrawer';
import './index.css';

/** 必须是 type 别名而不是 interface：CommonTable 的 F 要求隐式索引签名 */
type KeyFilters = {
  keyword?: string;
  status?: string;
};

const defaultFormatDate = (d: string | null): string => {
  if (!d) return '-';
  try {
    return new Date(d).toISOString().slice(0, 10);
  } catch {
    return d;
  }
};

/** 4 态派生：revoked（不可逆终态）> expired > disabled > active（v2.1 对齐写 Key 语义） */
const deriveStatus = (r: McpKeyItem): 'active' | 'disabled' | 'expired' | 'revoked' => {
  if (r.status === 'disabled' || r.status === 'expired' || r.status === 'revoked') return r.status;
  if (r.revoked_at) return 'revoked';
  if (r.expires_at && new Date(r.expires_at).getTime() < Date.now()) return 'expired';
  if (r.disabled_at) return 'disabled';
  return 'active';
};

/** 服务访问 Key 列表 Tab（v2.1 统一 Key：授权能力/写入范围/按状态操作/revoked 终态优先） */
export default function McpKeysTab() {
  const { t } = useTranslation();
  // 权限（shell 下发）：由共享 hook usePermission 提供，按钮级判断交给 PermButton
  const { permissions: userPermissions } = usePermission();
  const tableRef = useRef<CommonTableRef>(null);
  const [error, setError] = useState<string | null>(null);
  /** 接口返回的全量条数：区分「一个 Key 都没有」与「筛选没命中」 */
  const [totalCount, setTotalCount] = useState<number | null>(null);
  // 知识库列表（写入范围列名称映射）
  const [kbList, setKbList] = useState<KnowledgeBaseBrief[]>([]);

  const keyDrawerRef = useRef<McpKeyDrawerHandle>(null);
  const recreateRef = useRef<RecreateKeyModalHandle>(null);
  const detailRef = useRef<McpKeyDetailDrawerHandle>(null);

  /** KB id → 名称（写入范围展示） */
  const kbNameMap = useMemo(() => {
    const m = new Map<string, string>();
    kbList.forEach((k) => m.set(k.id, k.name));
    return m;
  }, [kbList]);

  /**
   * 取数 / 过滤全在这里 —— CommonTable 只认这一个数据出口。
   * ⚠️ 关键词与状态都从参数取，不要读页面 state：fetch 存在 ref 里，闭包不参与重渲染。
   * ⚠️ 本页不分页（见下方 pagination={false}），所以一次性返回全部过滤结果、忽略 page/pageSize ——
   *    若在这里按 pageSize 切片，超出首屏条数的 Key 会永远看不到（没有翻页可到达）。
   */
  const fetchRows = useCallback(
    async ({ keyword, status }: TableQueryParams<KeyFilters>) => {
      const q = (keyword ?? '').trim().toLocaleLowerCase();
      try {
        const result = await listMcpKeys();
        const all = result.items || [];
        setTotalCount(all.length);
        setError(null);
        const rows = all.filter((k) => {
          if (q && !`${k.name || ''} ${k.key_prefix || ''}`.toLocaleLowerCase().includes(q)) return false;
          if (status && deriveStatus(k) !== status) return false;
          return true;
        });
        return { list: rows, total: rows.length };
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : t('mcpKeyManagement.loadFailed'));
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
        placeholder: t('mcpKeyManagement.searchPlaceholder'),
      },
      {
        key: 'status',
        label: t('mcpKeyManagement.status'),
        type: 'select',
        placeholder: t('mcpKeyManagement.statusAll'),
        options: [
          { value: 'active', label: t('mcpKeyManagement.activeStatus') },
          { value: 'disabled', label: t('mcpKeyManagement.disabledStatus') },
          { value: 'expired', label: t('mcpKeyManagement.expiredStatus') },
          { value: 'revoked', label: t('mcpKeyManagement.revokedStatus') },
        ],
      },
    ],
    [t],
  );

  // 知识库列表（KB id → 名称映射，一次性加载）
  useEffect(() => {
    listKnowledgeBases()
      .then(setKbList)
      .catch(() => setKbList([]));
  }, []);

  /** 授权能力聚合：可调用 N · 仅查看 N，含写入授权追加「知识写入」（legacy write/* 兜底） */
  const renderCapabilities = (record: McpKeyItem) => {
    const sp = record.service_permissions || [];
    const callN = sp.filter((s) => s.permission_level === 'call').length;
    const viewN = sp.filter((s) => s.permission_level === 'view').length;
    // 旧写 Key 并入统一 mcp-keys 时 service_permissions 可能带 write/* 级权限 → 视同「知识写入」
    const legacyWriteN = sp.filter((s) => s.permission_level !== 'call' && s.permission_level !== 'view').length;
    const hasWrite = !!record.write_grants?.length || legacyWriteN > 0;
    const parts: React.ReactNode[] = [];
    if (callN > 0) {
      parts.push(
        <span className="mcp-chip is-call" key="call">
          {t('mcpKeyManagement.callCount', { n: callN })}
        </span>,
      );
    }
    if (viewN > 0) {
      parts.push(
        <span className="mcp-chip is-view" key="view">
          {t('mcpKeyManagement.viewCount', { n: viewN })}
        </span>,
      );
    }
    if (hasWrite) {
      parts.push(
        <span className="mcp-chip is-write" key="write">
          {t('mcpKeyManagement.writeCapability')}
        </span>,
      );
    }
    if (parts.length === 0) return <span className="mcp-cell-muted">-</span>;
    return <div className="mcp-chip-row">{parts}</div>;
  };

  /** 写入范围聚合：KB名(全部) / KB名(N 目录)，多 KB 折叠（复用写 Key 渲染逻辑） */
  const renderWriteScope = (record: McpKeyItem) => {
    const grants = record.write_grants || [];
    if (grants.length === 0) return <span className="mcp-cell-muted">-</span>;
    const renderOne = (g: WriteGrant) => {
      const kbName = kbNameMap.get(g.kb) ?? g.kb;
      return g.mode === 'all'
        ? `${kbName}(${t('mcpKeyManagement.writeAll')})`
        : `${kbName}(${g.directories?.length ?? 0} ${t('mcpKeyManagement.directoryUnit')})`;
    };
    const shown = grants.slice(0, 2);
    const rest = grants.slice(2);
    return (
      <div className="mcp-chip-row">
        {shown.map((g, i) => (
          <span className="mcp-chip" key={i}>
            {renderOne(g)}
          </span>
        ))}
        {rest.length > 0 && (
          <Tooltip title={rest.map(renderOne).join('；')}>
            <span className="mcp-chip is-more">+{rest.length}</span>
          </Tooltip>
        )}
      </div>
    );
  };

  /** 停用/启用（toggle 可逆，key 不变；已撤销 Key 调 toggle → 409） */
  const handleToggle = async (record: McpKeyItem) => {
    try {
      await toggleMcpKey(record.id);
      message.success(t('mcpKeyManagement.toggleSuccess'));
      tableRef.current?.refresh();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
    }
  };

  const confirmDisable = (record: McpKeyItem) => {
    Modal.confirm({
      title: t('mcpKeyManagement.disableTitle'),
      content: t('mcpKeyManagement.disableConfirm', { name: record.name || record.key_prefix }),
      okText: t('mcpKeyManagement.disableBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: () => handleToggle(record),
    });
  };

  /** 撤销（不可逆终态） */
  const confirmRevoke = (record: McpKeyItem) => {
    Modal.confirm({
      title: t('mcpKeyManagement.revokeTitle'),
      content: t('mcpKeyManagement.revokeConfirm', { name: record.name || record.key_prefix }),
      okText: t('mcpKeyManagement.revokeBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await revokeMcpKey(record.id);
          message.success(t('mcpKeyManagement.revokeSuccess'));
          tableRef.current?.refresh();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  /** 删除（软删除，不可逆；删除后 Key 立即失效且列表不再展示） */
  const confirmDelete = (record: McpKeyItem) => {
    Modal.confirm({
      title: t('mcpKeyManagement.deleteTitle'),
      content: t('mcpKeyManagement.deleteConfirm', { name: record.name || record.key_prefix }),
      okText: t('mcpKeyManagement.deleteBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteMcpKey(record.id);
          message.success(t('mcpKeyManagement.deleteSuccess'));
          tableRef.current?.refresh();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  /** 4 态状态渲染：圆点 + 文案，避免彩色 Tag 抢行内视觉 */
  const renderStatus = (record: McpKeyItem) => {
    switch (deriveStatus(record)) {
      case 'disabled':
        return <span className="mcp-status is-off">{t('mcpKeyManagement.disabledStatus')}</span>;
      case 'expired':
        return <span className="mcp-status is-expired">{t('mcpKeyManagement.expiredStatus')}</span>;
      case 'revoked':
        return <span className="mcp-status is-revoked">{t('mcpKeyManagement.revokedStatus')}</span>;
      default:
        return <span className="mcp-status is-active">{t('mcpKeyManagement.activeStatus')}</span>;
    }
  };

  /** 操作列：查看常显，其余动作按「状态 + 权限」收进「更多」下拉 */
  const renderActions = (record: McpKeyItem) => {
    const status = deriveStatus(record);
    const canManage = userPermissions.includes('mcp:key:manage');
    const moreItems: MenuProps['items'] = [];
    if (status === 'expired') {
      moreItems.push({
        key: 'recreate',
        label: t('mcpKeyManagement.recreateBtn'),
        disabled: !canManage,
        onClick: () => recreateRef.current?.open(record),
      });
    }
    if (status === 'active' || status === 'disabled') {
      moreItems.push({
        key: 'edit',
        label: t('mcpKeyManagement.editBtn'),
        disabled: !canManage,
        onClick: () => keyDrawerRef.current?.openEdit(record),
      });
    }
    if (status === 'disabled') {
      moreItems.push({
        key: 'enable',
        label: t('mcpKeyManagement.enableBtn'),
        disabled: !canManage,
        onClick: () => handleToggle(record),
      });
    }
    if (status === 'active') {
      moreItems.push({
        key: 'disable',
        label: t('mcpKeyManagement.disableBtn'),
        disabled: !canManage,
        onClick: () => confirmDisable(record),
      });
    }
    if (status === 'active' || status === 'disabled') {
      moreItems.push({
        key: 'revoke',
        label: t('mcpKeyManagement.revokeBtn'),
        danger: true,
        disabled: !canManage,
        onClick: () => confirmRevoke(record),
      });
    }
    moreItems.push({ type: 'divider' }, {
      key: 'delete',
      label: t('mcpKeyManagement.deleteBtn'),
      danger: true,
      disabled: !canManage,
      onClick: () => confirmDelete(record),
    });

    return (
      <div className="mcp-row-acts">
        <PermButton
          type="link"
          size="small"
          permissions={userPermissions}
          requiredPerm={['mcp:key:view', 'mcp:key:manage']}
          onClick={() => detailRef.current?.open(record)}
        >
          {t('mcpKeyManagement.viewBtn')}
        </PermButton>
        <Dropdown menu={{ items: moreItems }} trigger={['click']} placement="bottomRight">
          <Tooltip title={t('mcpKeyManagement.moreActions')}>
            <Button type="text" className="mcp-more-btn" icon={<EllipsisOutlined />} />
          </Tooltip>
        </Dropdown>
      </div>
    );
  };

  const columns: ColumnsType<McpKeyItem> = [
    {
      title: t('mcpKeyManagement.nameLabel'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (v: string) => v || <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpKeyManagement.keyPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      width: 140,
      render: (v: string) => (v ? <Typography.Text code>{v}</Typography.Text> : '-'),
    },
    {
      title: t('mcpKeyManagement.colCapability'),
      key: 'capability',
      width: 170,
      render: (_: unknown, record: McpKeyItem) => renderCapabilities(record),
    },
    {
      title: t('mcpKeyManagement.colWriteScope'),
      key: 'write_scope',
      width: 190,
      render: (_: unknown, record: McpKeyItem) => renderWriteScope(record),
    },
    {
      title: t('mcpKeyManagement.status'),
      key: 'status',
      width: 96,
      render: (_: unknown, record: McpKeyItem) => renderStatus(record),
    },
    {
      title: t('mcpKeyManagement.colCreatedAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 112,
      render: (v: string | null) => (v ? defaultFormatDate(v) : '-'),
    },
    {
      title: t('mcpKeyManagement.noteLabel'),
      dataIndex: 'note',
      key: 'note',
      width: 160,
      ellipsis: true,
      render: (v: string | null) => (v ? v : <span style={{ color: '#94a3b8' }}>-</span>),
    },
    {
      title: t('mcpKeyManagement.actions'),
      key: 'actions',
      width: 120,
      render: (_: unknown, record: McpKeyItem) => renderActions(record),
    },
  ];

  if (error) {
    return (
      <Result
        status="error"
        title={t('mcpKeyManagement.loadFailed')}
        subTitle={error}
        extra={
          // ⚠️ 不能调 tableRef.current.refresh()：错误态下 CommonTable 已卸载、ref 为 null。
          <Button type="primary" icon={<ReloadOutlined />} onClick={() => setError(null)}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="mcp-tab-body">
      <CommonTable<McpKeyItem, KeyFilters>
        ref={tableRef}
        rowKey="id"
        filters={filters}
        columns={columns}
        fetch={fetchRows}
        size="middle"
        // 迁移前即不分页：一次性取全量、全部铺开（列表长也不加翻页）；列宽合计 1138，容器不足时左右滑动
        pagination={false}
        scroll={{ x: 1180 }}
        // 「一个 Key 都没有」才给创建引导；筛选没命中只给一句提示
        emptyText={
          totalCount === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyText')}>
              <PermButton
                type="primary"
                icon={<PlusOutlined />}
                permissions={userPermissions}
                requiredPerm="mcp:key:manage"
                onClick={() => keyDrawerRef.current?.open()}
              >
                {t('mcpKeyManagement.createFirst')}
              </PermButton>
            </Empty>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyFilteredText')} />
          )
        }
        toolbar={
          <PermButton
            type="primary"
            icon={<PlusOutlined />}
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => keyDrawerRef.current?.open()}
          >
            {t('mcpKeyManagement.createBtn')}
          </PermButton>
        }
      />

      {/* 创建 / 编辑 / 重新创建 / 详情 */}
      <McpKeyDrawer ref={keyDrawerRef} onSuccess={() => tableRef.current?.refresh()} />
      <RecreateKeyModal ref={recreateRef} onSuccess={() => tableRef.current?.refresh()} />
      <McpKeyDetailDrawer ref={detailRef} kbNameMap={kbNameMap} />
    </div>
  );
}
