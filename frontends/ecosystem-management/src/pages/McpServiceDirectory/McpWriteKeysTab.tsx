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
import { PlusOutlined, SearchOutlined, ReloadOutlined, FileTextOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  listMcpWriteKeys,
  toggleMcpWriteKey,
  revokeMcpWriteKey,
  WRITE_KEY_PREFIX,
  WRITE_TOOL_NAME,
  type WriteKeyItem,
} from '@/api/mcpWriteKeys';
import { listKnowledgeBases, listSpaces, type KnowledgeBaseBrief } from '@/api/spaces';
import { McpWriteKeyDrawer, type McpWriteKeyDrawerHandle } from './McpWriteKeyDrawer';
import { McpWriteKeyDetailDrawer, type McpWriteKeyDetailDrawerHandle } from './McpWriteKeyDetailDrawer';
import { McpWriteKeyEditModal, type McpWriteKeyEditModalHandle } from './McpWriteKeyEditModal';
import './index.css';

const formatDate = (d: string | null): string => {
  if (!d) return '-';
  try {
    return new Date(d).toISOString().slice(0, 10);
  } catch {
    return d;
  }
};

type KeyStatus = 'active' | 'disabled' | 'expired' | 'revoked';

/** 4 态派生：revoked > expired > disabled > active（⚠️ 与 Phase 16 服务访问 Key 相反） */
const deriveStatus = (r: WriteKeyItem): KeyStatus => {
  if (r.status === 'revoked' || r.status === 'expired' || r.status === 'disabled') return r.status;
  if (r.revoked_at) return 'revoked';
  if (r.expires_at && new Date(r.expires_at).getTime() < Date.now()) return 'expired';
  if (r.disabled_at) return 'disabled';
  return 'active';
};

/** MCP 知识写入 Tab — hero 卡 + 写 Key 列表（WRITE-01/03） */
export default function McpWriteKeysTab() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState<WriteKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  // 知识库列表（授权范围列名称映射 + 写入范围编辑器数据源）
  const [kbList, setKbList] = useState<KnowledgeBaseBrief[]>([]);
  // 领域空间 ID → 名称（授权范围展示 / 同 space 约束提示）
  const [spaceNameMap, setSpaceNameMap] = useState<Record<string, string>>({});

  const drawerRef = useRef<McpWriteKeyDrawerHandle>(null);
  const detailRef = useRef<McpWriteKeyDetailDrawerHandle>(null);
  const editRef = useRef<McpWriteKeyEditModalHandle>(null);

  const kbNameMap = useMemo(() => {
    const m = new Map<string, string>();
    kbList.forEach((k) => m.set(k.id, k.name));
    return m;
  }, [kbList]);

  /** 加载写 Key 列表 */
  const loadKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpWriteKeys();
      setKeys(result.items || []);
      setLoaded(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('mcpWriteKeys.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!loaded) loadKeys();
  }, [loaded, loadKeys]);

  // 知识库列表 + 空间名称映射（一次性加载）
  useEffect(() => {
    listKnowledgeBases()
      .then(setKbList)
      .catch(() => setKbList([]));
    listSpaces()
      .then((items) => {
        const m: Record<string, string> = {};
        items.forEach((s) => {
          m[s.id] = s.name ?? s.id;
        });
        setSpaceNameMap(m);
      })
      .catch(() => {
        // 失败静默，映射为空
      });
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

  /** 授权范围聚合：KB名(全部) / KB名(N 目录)，多 KB 折叠 */
  const renderGrants = (record: WriteKeyItem) => {
    const grants = record.grants || [];
    if (grants.length === 0) return <span style={{ color: '#94a3b8' }}>-</span>;
    const renderOne = (g: (typeof grants)[number]) => {
      const kbName = g.kb ? kbNameMap.get(g.kb) ?? g.kb : '-';
      return g.mode === 'all'
        ? `${kbName}(${t('mcpWriteKeys.modeAll')})`
        : `${kbName}(${g.directories?.length ?? 0} ${t('mcpWriteKeys.directoryUnit')})`;
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

  const renderStatus = (record: WriteKeyItem) => {
    switch (deriveStatus(record)) {
      case 'revoked':
        return <Tag color="error">{t('mcpWriteKeys.statusRevoked')}</Tag>;
      case 'expired':
        return <Tag color="orange">{t('mcpWriteKeys.statusExpired')}</Tag>;
      case 'disabled':
        return <Tag>{t('mcpWriteKeys.statusDisabled')}</Tag>;
      default:
        return <Tag color="success">{t('mcpWriteKeys.statusActive')}</Tag>;
    }
  };

  /** 停用/启用（toggle 可逆，key 不变；已撤销 Key 调 toggle 后端 409） */
  const handleToggle = async (record: WriteKeyItem) => {
    try {
      await toggleMcpWriteKey(record.id);
      message.success(t('mcpWriteKeys.toggleSuccess'));
      loadKeys();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('mcpWriteKeys.operationFailed'));
    }
  };

  const confirmDisable = (record: WriteKeyItem) => {
    Modal.confirm({
      title: t('mcpWriteKeys.disableTitle'),
      content: t('mcpWriteKeys.disableConfirm', { name: record.name }),
      okText: t('mcpWriteKeys.disableBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: () => handleToggle(record),
    });
  };

  /** 撤销（不可逆终态） */
  const confirmRevoke = (record: WriteKeyItem) => {
    Modal.confirm({
      title: t('mcpWriteKeys.revokeTitle'),
      content: t('mcpWriteKeys.revokeConfirm', { name: record.name }),
      okText: t('mcpWriteKeys.revokeBtn'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await revokeMcpWriteKey(record.id);
          message.success(t('mcpWriteKeys.revokeSuccess'));
          loadKeys();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpWriteKeys.operationFailed'));
        }
      },
    });
  };

  const columns: ColumnsType<WriteKeyItem> = [
    {
      title: t('mcpWriteKeys.colName'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (v: string) => v || <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpWriteKeys.keyPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      width: 150,
      render: (v: string) =>
        v ? (
          <Typography.Text code>
            {WRITE_KEY_PREFIX}
            {v}
          </Typography.Text>
        ) : (
          '-'
        ),
    },
    {
      title: t('mcpWriteKeys.colGrants'),
      key: 'grants',
      width: 200,
      render: (_: unknown, record: WriteKeyItem) => renderGrants(record),
    },
    {
      title: t('mcpWriteKeys.expiryLabel'),
      dataIndex: 'expires_at',
      key: 'expires_at',
      width: 110,
      render: (v: string | null) => (v ? formatDate(v) : t('mcpWriteKeys.expiryForever')),
    },
    {
      title: t('mcpWriteKeys.status'),
      key: 'status',
      width: 90,
      render: (_: unknown, record: WriteKeyItem) => renderStatus(record),
    },
    {
      title: t('mcpWriteKeys.colActions'),
      key: 'actions',
      width: 220,
      render: (_: unknown, record: WriteKeyItem) => {
        const status = deriveStatus(record);
        // 已撤销为不可逆终态：仅详情（toggle/编辑/撤销均置灰）
        if (status === 'revoked') {
          return (
            <Space size={0}>
              <Button type="link" size="small" onClick={() => detailRef.current?.open(record)}>
                {t('mcpWriteKeys.detailBtn')}
              </Button>
            </Space>
          );
        }
        // expired 无重新创建/无 toggle（只能重新走创建流程），仅保留编辑/撤销/详情
        const canToggle = status === 'active' || status === 'disabled';
        return (
          <Space size={0}>
            <Button type="link" size="small" onClick={() => editRef.current?.openEdit(record)}>
              {t('mcpWriteKeys.editBtn')}
            </Button>
            {canToggle &&
              (status === 'disabled' ? (
                <Button type="link" size="small" onClick={() => handleToggle(record)}>
                  {t('mcpWriteKeys.enableBtn')}
                </Button>
              ) : (
                <Button type="link" size="small" danger onClick={() => confirmDisable(record)}>
                  {t('mcpWriteKeys.disableBtn')}
                </Button>
              ))}
            <Button type="link" size="small" danger onClick={() => confirmRevoke(record)}>
              {t('mcpWriteKeys.revokeBtn')}
            </Button>
            <Button type="link" size="small" onClick={() => detailRef.current?.open(record)}>
              {t('mcpWriteKeys.detailBtn')}
            </Button>
          </Space>
        );
      },
    },
  ];

  // ── 加载态 ──
  if (!loaded) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 200 }}>
        <Spin size="large" />
      </div>
    );
  }

  // ── 错误态 ──
  if (error) {
    return (
      <Result
        status="error"
        title={t('mcpWriteKeys.loadFailed')}
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
      {/* hero 卡：系统服务元信息（Tool / 类型 / 已授权 Key 数） */}
      <div className="mcp-svc-card-head">
        <div className="mcp-svc-card-head-inner">
          <FileTextOutlined className="mcp-svc-card-icon" />
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <h2 style={{ margin: 0 }}>{t('mcpWriteKeys.heroTitle')}</h2>
              <Tag color="blue">{t('mcpWriteKeys.systemService')}</Tag>
            </div>
            <p>{t('mcpWriteKeys.heroDesc')}</p>
          </div>
        </div>
        <div className="mcp-write-hero-stats">
          <div className="mcp-write-hero-stat">
            <div className="mcp-write-hero-stat-value">
              <Typography.Text code>{WRITE_TOOL_NAME}</Typography.Text>
            </div>
            <div className="mcp-write-hero-stat-label">{t('mcpWriteKeys.heroTool')}</div>
          </div>
          <div className="mcp-write-hero-divider" />
          <div className="mcp-write-hero-stat">
            <div className="mcp-write-hero-stat-value">{keys.length}</div>
            <div className="mcp-write-hero-stat-label">{t('mcpWriteKeys.heroKeyCount')}</div>
          </div>
        </div>
      </div>

      {/* 工具栏：搜索 + 状态筛选 + 刷新 / 创建 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 12,
          flexWrap: 'wrap',
          gap: 8,
        }}
      >
        <Space size={12} wrap>
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
            placeholder={t('mcpWriteKeys.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 220 }}
          />
          <Select
            value={statusFilter || undefined}
            onChange={(v) => setStatusFilter(v || '')}
            placeholder={t('mcpWriteKeys.statusAll')}
            allowClear
            style={{ width: 120 }}
            options={[
              { value: 'active', label: t('mcpWriteKeys.statusActive') },
              { value: 'disabled', label: t('mcpWriteKeys.statusDisabled') },
              { value: 'expired', label: t('mcpWriteKeys.statusExpired') },
              { value: 'revoked', label: t('mcpWriteKeys.statusRevoked') },
            ]}
          />
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadKeys}>
            {t('common.refresh')}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => drawerRef.current?.open()}>
            {t('mcpWriteKeys.createBtn')}
          </Button>
        </Space>
      </div>

      {keys.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpWriteKeys.emptyText')}>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => drawerRef.current?.open()}>
            {t('mcpWriteKeys.createFirst')}
          </Button>
        </Empty>
      ) : filtered.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpWriteKeys.emptyFilteredText')} />
      ) : (
        <Table<WriteKeyItem>
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={false}
          size="middle"
          loading={loading}
          bordered
        />
      )}

      {/* 创建 / 详情 / 编辑 */}
      <McpWriteKeyDrawer ref={drawerRef} kbList={kbList} spaceNameMap={spaceNameMap} onSuccess={loadKeys} />
      <McpWriteKeyDetailDrawer ref={detailRef} kbList={kbList} spaceNameMap={spaceNameMap} />
      <McpWriteKeyEditModal ref={editRef} kbList={kbList} spaceNameMap={spaceNameMap} onSuccess={loadKeys} />
    </div>
  );
}
