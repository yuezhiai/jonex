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
  message,
} from 'antd';
import { PlusOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { listMcpKeys, deleteMcpKey, type McpKeyItem } from '@/api/mcpKeys';
import { toggleKeyStatus } from '@/api/domainServices';
import { McpKeyDrawer, type McpKeyDrawerHandle } from './McpKeyDrawer';
import { RecreateKeyModal, type RecreateKeyModalHandle } from './RecreateKeyModal';
import { listMcpServices } from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import './index.css';

const defaultFormatDate = (d: string | null): string => {
  if (!d) return '-';
  try {
    return new Date(d).toISOString().slice(0, 10);
  } catch {
    return d;
  }
};

/** 4 态派生：expired > disabled > revoked > active（后端 status 优先） */
const deriveStatus = (r: McpKeyItem): 'active' | 'disabled' | 'expired' | 'revoked' => {
  if (r.status === 'disabled' || r.status === 'expired' || r.status === 'revoked') return r.status;
  if (r.expires_at && new Date(r.expires_at).getTime() < Date.now()) return 'expired';
  if (r.disabled_at) return 'disabled';
  if (r.revoked_at) return 'revoked';
  return 'active';
};

/** 存量 write/* 遗留权限（需降级角标） */
const hasLegacyPermission = (r: McpKeyItem): boolean => {
  if ((r.permissions || []).some((p) => p === 'write' || (p as string) === '*')) return true;
  if ((r.service_permissions || []).some((s) => s.permission_level === 'write' || s.permission_level === '*')) return true;
  return false;
};

/** 服务访问 Key 列表 Tab（Phase 16：4 态列/筛选/note/遗留角标/重新创建） */
export default function McpKeysTab() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState<McpKeyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  // 领域空间下拉选项（供 Key 创建弹窗使用）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);
  // 领域服务 ID → 名称映射（列表展示用，来源全量服务确保都能映射名称）
  const [serviceNameMap, setServiceNameMap] = useState<Map<string, string>>(new Map());

  const keyDrawerRef = useRef<McpKeyDrawerHandle>(null);
  const recreateRef = useRef<RecreateKeyModalHandle>(null);

  /** 加载 Key 列表 */
  const loadKeys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpKeys();
      setKeys(result.items || []);
      setLoaded(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('mcpKeyManagement.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!loaded) loadKeys();
  }, [loaded, loadKeys]);

  // 领域服务名称映射（含未发布服务，确保 service_ids 都能显示名称）
  useEffect(() => {
    listMcpServices()
      .then((result) => {
        const map = new Map<string, string>();
        result.items.forEach((s) => map.set(s.id, s.name));
        setServiceNameMap(map);
      })
      .catch(() => {
        // 失败静默，映射为空
      });
  }, []);

  // 领域空间下拉选项（供 Key 创建弹窗）
  useEffect(() => {
    listSpaces()
      .then((items) => {
        setSpaceOptions(items.map((s) => ({ label: s.name ?? s.id, value: s.id })));
      })
      .catch(() => {
        // 失败静默，下拉为空
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

  // ── 删除确认 ──
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
          await loadKeys();
        } catch (err: unknown) {
          message.error(err instanceof Error ? err.message : t('mcpKeyManagement.operationFailed'));
        }
      },
    });
  };

  /** 停用/启用（toggle 可逆，恢复原 Key） */
  const handleToggle = async (record: McpKeyItem) => {
    const serviceId = record.service_ids?.[0] || '';
    try {
      await toggleKeyStatus(serviceId, record.id);
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

  /** 权限渲染：精确映射 view=金/call=蓝/write=绿/*=紫，存量 write/* 保留遗留角标 */
  const renderPermissions = (record: McpKeyItem) => {
    const legacy = hasLegacyPermission(record);
    const perms = record.permissions || [];
    if (perms.length === 0 && !legacy) return <span style={{ color: '#94a3b8' }}>-</span>;
    const tags = perms.map((p) => {
      // 存量 Key 的 permissions 可能含 '*'（全部），McpKeyPermission 类型不含，显式转 string
      const perm = p as string;
      switch (perm) {
        case 'view':
          return <Tag color="gold">{t('mcpKeyManagement.permissionView')}</Tag>;
        case 'call':
          return <Tag color="blue">{t('mcpKeyManagement.permissionCall')}</Tag>;
        case 'write':
          return <Tag color="green">{t('mcpKeyManagement.permissionWrite')}</Tag>;
        case '*':
          return <Tag color="purple">{t('mcpKeyManagement.permissionAll')}</Tag>;
        default:
          return <Tag>{p}</Tag>;
      }
    });
    if (legacy) {
      tags.push(
        <Tag color="orange" key="legacy">
          {t('mcpKeyManagement.legacyPermissionTag')}
        </Tag>,
      );
    }
    return (
      <Space size={4} wrap>
        {tags}
      </Space>
    );
  };

  const columns: ColumnsType<McpKeyItem> = [
    {
      title: t('mcpKeyManagement.nameLabel'),
      dataIndex: 'name',
      key: 'name',
      width: 140,
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
      title: t('mcpKeyManagement.serviceScopeLabel'),
      dataIndex: 'service_ids',
      key: 'service_ids',
      width: 160,
      render: (_: unknown, record: McpKeyItem) => {
        const ids = record.service_ids || [];
        if (ids.length === 0) return <Tag>{t('mcpKeyManagement.allServices')}</Tag>;
        return (
          <Space size={4} wrap>
            {ids.slice(0, 2).map((id) => (
              <Tag key={id}>{serviceNameMap.get(id) || id}</Tag>
            ))}
            {ids.length > 2 && <Tag>+{ids.length - 2}</Tag>}
          </Space>
        );
      },
    },
    {
      title: t('mcpKeyManagement.expiryLabel'),
      dataIndex: 'expires_at',
      key: 'expires_at',
      width: 110,
      render: (v: string | null) => (v ? defaultFormatDate(v) : t('mcpKeyManagement.expiryForever')),
    },
    {
      title: t('mcpKeyManagement.noteLabel'),
      dataIndex: 'note',
      key: 'note',
      width: 160,
      render: (v: string | null | undefined) => v || <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpKeyManagement.permissions'),
      key: 'permissions',
      width: 130,
      render: (_: unknown, record: McpKeyItem) => renderPermissions(record),
    },
    {
      title: t('mcpKeyManagement.status'),
      key: 'status',
      width: 100,
      render: (_: unknown, record: McpKeyItem) => renderStatus(record),
    },
    {
      title: t('mcpKeyManagement.actions'),
      key: 'actions',
      width: 220,
      render: (_: unknown, record: McpKeyItem) => {
        const status = deriveStatus(record);
        return (
          <Space size={0}>
            <Button type="link" size="small" onClick={() => keyDrawerRef.current?.openEdit(record)}>
              {t('mcpKeyManagement.editBtn')}
            </Button>
            <Button type="link" size="small" danger onClick={() => confirmDelete(record)}>
              {t('mcpKeyManagement.deleteBtn')}
            </Button>
            {status === 'active' && (
              <Button type="link" size="small" danger onClick={() => confirmDisable(record)}>
                {t('mcpKeyManagement.disableBtn')}
              </Button>
            )}
            {status === 'disabled' && (
              <Button type="link" size="small" onClick={() => handleToggle(record)}>
                {t('mcpKeyManagement.enableBtn')}
              </Button>
            )}
            {status === 'expired' && (
              <Button type="link" size="small" onClick={() => recreateRef.current?.open(record)}>
                {t('mcpKeyManagement.recreateBtn')}
              </Button>
            )}
          </Space>
        );
      },
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
          <Button type="primary" icon={<PlusOutlined />} onClick={() => keyDrawerRef.current?.open()}>
            {t('mcpKeyManagement.createBtn')}
          </Button>
        </Space>
      </div>

      {keys.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpKeyManagement.emptyText')}>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => keyDrawerRef.current?.open()}>
            {t('mcpKeyManagement.createFirst')}
          </Button>
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
          bordered
        />
      )}

      {/* 创建 / 编辑 / 重新创建 抽屉 */}
      <McpKeyDrawer ref={keyDrawerRef} onSuccess={loadKeys} spaceOptions={spaceOptions} />
      <RecreateKeyModal ref={recreateRef} onSuccess={loadKeys} />
    </div>
  );
}
