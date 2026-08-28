import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Table,
  Tag,
  Typography,
  Input,
  Select,
  Empty,
  Space,
  Button,
  Spin,
  Result,
  Modal,
  Tooltip,
  message,
} from 'antd';
import { PlusOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { listMcpKeys, revokeMcpKey, toggleMcpKey, deleteMcpKey, type McpKeyItem } from '@/api/mcpKeys';
import type { WriteGrant } from '@/api/mcpKeys';
import { listKnowledgeBases, type KnowledgeBaseBrief } from '@/api/spaces';
import { PermButton, usePermission } from '@jonex/shared-lib';
import { McpKeyDrawer, type McpKeyDrawerHandle } from './McpKeyDrawer';
import { RecreateKeyModal, type RecreateKeyModalHandle } from './RecreateKeyModal';
import McpKeyDetailDrawer, { type McpKeyDetailDrawerHandle } from './McpKeyDetailDrawer';
import './index.css';

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
  const [keys, setKeys] = useState<McpKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
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

  /** 加载 Key 列表 */
  const loadKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpKeys();
      setKeys(result.items || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('mcpKeyManagement.loadFailed'));
    } finally {
      setLoading(false);
      // 成功/失败都必须结束首屏 Spin：失败时让 error Result（含重试按钮）可达
      setLoaded(true);
    }
  }, [t]);

  useEffect(() => {
    if (!loaded) loadKeys();
  }, [loaded, loadKeys]);

  // 知识库列表（KB id → 名称映射，一次性加载）
  useEffect(() => {
    listKnowledgeBases()
      .then(setKbList)
      .catch(() => setKbList([]));
  }, []);

  // 搜索 + 状态筛选（客户端过滤）
  const filtered = useMemo(() => {
    const q = search.trim().toLocaleLowerCase();
    return keys.filter((k) => {
      if (q && !`${k.name || ''} ${k.key_prefix || ''}`.toLocaleLowerCase().includes(q)) return false;
      if (statusFilter && deriveStatus(k) !== statusFilter) return false;
      return true;
    });
  }, [keys, search, statusFilter]);

  /** 授权能力聚合：可调用 N · 仅查看 N，含写入授权追加「知识写入」（legacy write/* 兜底） */
  const renderCapabilities = (record: McpKeyItem) => {
    const sp = record.service_permissions || [];
    const callN = sp.filter((s) => s.permission_level === 'call').length;
    const viewN = sp.filter((s) => s.permission_level === 'view').length;
    // 旧写 Key 并入统一 mcp-keys 时 service_permissions 可能带 write/* 级权限 → 视同「知识写入」
    const legacyWriteN = sp.filter((s) => s.permission_level !== 'call' && s.permission_level !== 'view').length;
    const hasWrite = !!record.write_grants?.length || legacyWriteN > 0;
    const parts: React.ReactNode[] = [];
    if (callN > 0) parts.push(<Tag color="blue" key="call">{t('mcpKeyManagement.callCount', { n: callN })}</Tag>);
    if (viewN > 0) parts.push(<Tag color="gold" key="view">{t('mcpKeyManagement.viewCount', { n: viewN })}</Tag>);
    if (hasWrite) parts.push(<Tag color="green" key="write">{t('mcpKeyManagement.writeCapability')}</Tag>);
    if (parts.length === 0) return <span style={{ color: '#94a3b8' }}>-</span>;
    return <Space size={4} wrap>{parts}</Space>;
  };

  /** 写入范围聚合：KB名(全部) / KB名(N 目录)，多 KB 折叠（复用写 Key 渲染逻辑） */
  const renderWriteScope = (record: McpKeyItem) => {
    const grants = record.write_grants || [];
    if (grants.length === 0) return <span style={{ color: '#94a3b8' }}>-</span>;
    const renderOne = (g: WriteGrant) => {
      const kbName = kbNameMap.get(g.kb) ?? g.kb;
      return g.mode === 'all'
        ? `${kbName}(${t('mcpKeyManagement.writeAll')})`
        : `${kbName}(${g.directories?.length ?? 0} ${t('mcpKeyManagement.directoryUnit')})`;
    };
    const shown = grants.slice(0, 2);
    const rest = grants.slice(2);
    return (
      <Space size={4} wrap>
        {shown.map((g, i) => (
          <Tag key={i}>{renderOne(g)}</Tag>
        ))}
        {rest.length > 0 && (
          <Tooltip title={rest.map(renderOne).join('；')}>
            <Tag>+{rest.length}</Tag>
          </Tooltip>
        )}
      </Space>
    );
  };

  /** 停用/启用（toggle 可逆，key 不变；已撤销 Key 调 toggle → 409） */
  const handleToggle = async (record: McpKeyItem) => {
    try {
      await toggleMcpKey(record.id);
      message.success(t('mcpKeyManagement.toggleSuccess'));
      loadKeys();
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
          loadKeys();
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
          loadKeys();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  /** 4 态状态渲染 */
  const renderStatus = (record: McpKeyItem) => {
    switch (deriveStatus(record)) {
      case 'disabled':
        return <Tag>{t('mcpKeyManagement.disabledStatus')}</Tag>;
      case 'expired':
        return <Tag color="orange">{t('mcpKeyManagement.expiredStatus')}</Tag>;
      case 'revoked':
        return <Tag color="error">{t('mcpKeyManagement.revokedStatus')}</Tag>;
      default:
        return <Tag color="success">{t('mcpKeyManagement.activeStatus')}</Tag>;
    }
  };

  /** 操作列：每个按钮独立按「权限 + 状态」判断；权限判断由 PermButton 承载（无权限禁用并提示） */
  const renderActions = (record: McpKeyItem) => {
    const status = deriveStatus(record);
    return (
      <Space size={0}>
        <PermButton
          type="link"
          size="small"
          permissions={userPermissions}
          requiredPerm={['mcp:key:view', 'mcp:key:manage']}
          onClick={() => detailRef.current?.open(record)}
        >
          {t('mcpKeyManagement.viewBtn')}
        </PermButton>
        {status === 'expired' && (
          <PermButton
            type="link"
            size="small"
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => recreateRef.current?.open(record)}
          >
            {t('mcpKeyManagement.recreateBtn')}
          </PermButton>
        )}
        {(status === 'active' || status === 'disabled') && (
          <PermButton
            type="link"
            size="small"
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => keyDrawerRef.current?.openEdit(record)}
          >
            {t('mcpKeyManagement.editBtn')}
          </PermButton>
        )}
        {status === 'disabled' && (
          <PermButton
            type="link"
            size="small"
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => handleToggle(record)}
          >
            {t('mcpKeyManagement.enableBtn')}
          </PermButton>
        )}
        {status === 'active' && (
          <PermButton
            type="link"
            size="small"
            danger
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => confirmDisable(record)}
          >
            {t('mcpKeyManagement.disableBtn')}
          </PermButton>
        )}
        {(status === 'active' || status === 'disabled') && (
          <PermButton
            type="link"
            size="small"
            danger
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => confirmRevoke(record)}
          >
            {t('mcpKeyManagement.revokeBtn')}
          </PermButton>
        )}
        <PermButton
          type="link"
          size="small"
          danger
          permissions={userPermissions}
          requiredPerm="mcp:key:manage"
          onClick={() => confirmDelete(record)}
        >
          {t('mcpKeyManagement.deleteBtn')}
        </PermButton>
      </Space>
    );
  };

  const columns: ColumnsType<McpKeyItem> = [
    {
      title: t('mcpKeyManagement.nameLabel'),
      dataIndex: 'name',
      key: 'name',
      width: 150,
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
      width: 180,
      render: (_: unknown, record: McpKeyItem) => renderCapabilities(record),
    },
    {
      title: t('mcpKeyManagement.colWriteScope'),
      key: 'write_scope',
      width: 200,
      render: (_: unknown, record: McpKeyItem) => renderWriteScope(record),
    },
    {
      title: t('mcpKeyManagement.status'),
      key: 'status',
      width: 100,
      render: (_: unknown, record: McpKeyItem) => renderStatus(record),
    },
    {
      title: t('mcpKeyManagement.colCreatedAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (v: string | null) => (v ? defaultFormatDate(v) : '-'),
    },
    {
      title: t('mcpKeyManagement.noteLabel'),
      dataIndex: 'note',
      key: 'note',
      width: 180,
      ellipsis: true,
      render: (v: string | null) => (v ? v : <span style={{ color: '#94a3b8' }}>-</span>),
    },
    {
      title: t('mcpKeyManagement.actions'),
      key: 'actions',
      width: 300,
      render: (_: unknown, record: McpKeyItem) => renderActions(record),
    },
  ];

  // ── 状态覆盖 ──
  if (!loaded) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 200 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <Result
        status="error"
        title={t('mcpKeyManagement.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" icon={<ReloadOutlined />} onClick={loadKeys}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div>
      {/* 工具栏：搜索 + 状态筛选 + 刷新 / 创建 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Space size={12} wrap>
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
            placeholder={t('mcpKeyManagement.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 220 }}
          />
          <Select
            value={statusFilter || undefined}
            onChange={(v) => setStatusFilter(v || '')}
            placeholder={t('mcpKeyManagement.statusAll')}
            allowClear
            style={{ width: 120 }}
            options={[
              { value: 'active', label: t('mcpKeyManagement.activeStatus') },
              { value: 'disabled', label: t('mcpKeyManagement.disabledStatus') },
              { value: 'expired', label: t('mcpKeyManagement.expiredStatus') },
              { value: 'revoked', label: t('mcpKeyManagement.revokedStatus') },
            ]}
          />
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadKeys}>
            {t('common.refresh')}
          </Button>
          <PermButton
            type="primary"
            icon={<PlusOutlined />}
            permissions={userPermissions}
            requiredPerm="mcp:key:manage"
            onClick={() => keyDrawerRef.current?.open()}
          >
            {t('mcpKeyManagement.createBtn')}
          </PermButton>
        </Space>
      </div>

      {keys.length === 0 ? (
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
      ) : filtered.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyFilteredText')} />
      ) : (
        <Table<McpKeyItem>
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={false}
          size="middle"
          loading={loading}
        />
      )}

      {/* 创建 / 编辑 / 重新创建 / 详情 */}
      <McpKeyDrawer ref={keyDrawerRef} onSuccess={loadKeys} />
      <RecreateKeyModal ref={recreateRef} onSuccess={loadKeys} />
      <McpKeyDetailDrawer ref={detailRef} kbNameMap={kbNameMap} />
    </div>
  );
}
