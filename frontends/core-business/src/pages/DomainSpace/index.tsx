import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Input, Button, Table, Tag, Modal, Space, message, Result, Spin } from 'antd';
import {
  PlusOutlined,
  SearchOutlined,
  ReloadOutlined,
  TeamOutlined,
  WarningOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { emitSpacesInvalidated, emitSpaceChanged } from '@jonex/shell-sdk';
import { listSpaces, updateSpace, deleteSpace } from '../../api/domainSpace';
import { getSpaceStatusMap, type DomainSpace } from '../../types/domainSpace';
import { useStore } from '../../store';
import SpaceFormModal from '../../features/SpaceForm/SpaceFormModal';
import SpacePermissionModal from './SpacePermissionModal';
import type { SpacePermissionModalHandle } from './SpacePermissionModal';
import './index.scss';

export default function DomainSpace() {
  const { t } = useTranslation();
  const { global } = useStore();
  const navigate = useNavigate();
  const [spaces, setSpaces] = useState<DomainSpace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // modal state（表单由 SpaceFormModal 内部管理）
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<DomainSpace | null>(null);
  const permRef = useRef<SpacePermissionModalHandle>(null);
  const [deleting, setDeleting] = useState<DomainSpace | null>(null);
  // 租户级创建权限（后端按 service:write 计算）
  const [canCreateSpace, setCanCreateSpace] = useState(false);

  const loadSpaces = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listSpaces(0, 100);
      setSpaces(result.items);
      setCanCreateSpace(result.can_create_space ?? false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSpaces();
  }, [loadSpaces]);

  const filtered = spaces.filter((s) => {
    if (!search) return true;
    return s.name.includes(search) || (s.description || '').includes(search);
  });

  // ── CRUD handlers ──
  // 新建走独立页面（不再用弹窗）
  const openCreate = () => {
    navigate('/domain-space/new');
  };

  const openEdit = (space: DomainSpace) => {
    setEditing(space);
    setFormOpen(true);
  };

  // SpaceFormModal 保存成功回调（仅编辑）：同步本页列表 + 全局切换器列表 + 广播失效
  const handleSaved = async () => {
    setFormOpen(false);
    setEditing(null);
    await loadSpaces();
    await global.refreshSpaces();
    emitSpacesInvalidated();
  };

  const handleDelete = async () => {
    if (!deleting) return;
    setSubmitting(true);
    try {
      const wasCurrent = global.currentSpaceId === deleting.id;
      await deleteSpace(deleting.id);
      message.success(t('common.deleteSuccess'));
      setDeleting(null);
      await loadSpaces();
      // refreshSpaces 内部：若删的是当前空间会回落首个并持久化（broadcast:false）
      await global.refreshSpaces();
      if (wasCurrent) {
        // 广播新选中，让 Shell / 其它标签页更新高亮与页面
        emitSpaceChanged(global.currentSpaceId);
      }
      emitSpacesInvalidated();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.deleteFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  // 状态切换：启用 → 禁用 → 启用；维护中 → 启用
  const toggleStatus = async (space: DomainSpace) => {
    let newStatus: DomainSpace['status'];
    if (space.status === 'active') {
      newStatus = 'disabled';
    } else {
      newStatus = 'active';
    }
    try {
      await updateSpace(space.id, { status: newStatus });
      setSpaces((prev) => prev.map((s) => (s.id === space.id ? { ...s, status: newStatus } : s)));
      const cfg = getSpaceStatusMap(t)[newStatus];
      message.success(t('domainSpace.statusChanged', { status: cfg.label }));
      await global.refreshSpaces();
      emitSpacesInvalidated();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.operationFailed'));
    }
  };

  // ── 权限弹窗（由 SpacePermissionModal 内部管理） ──
  const handlePermSaved = () => {
    loadSpaces();
  };

  const columns = [
    {
      title: t('domainSpace.name'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (v: string) => <span style={{ color: '#3b82f6', cursor: 'pointer' }}>{v}</span>,
    },
    {
      title: t('domainSpace.description'),
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (v: string | null) => v || '—',
    },
    {
      title: t('domainSpace.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: (v: string | null) => v?.slice(0, 16) || '—',
    },
    {
      title: t('domainSpace.status'),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string, r: DomainSpace) => {
        const cfg = getSpaceStatusMap(t)[v] || { label: v, color: 'default' };
        return (
          <Tag color={cfg.color} style={{ cursor: 'pointer' }} onClick={() => toggleStatus(r)}>
            {cfg.label}
          </Tag>
        );
      },
    },
    // 权限设置（无管理权时置灰禁用——后端最终校验）
    {
      title: t('domainSpace.permissionSettings'),
      key: 'permission',
      width: 110,
      render: (_: unknown, r: DomainSpace) => (
        <span
          className="yx-perm-badge"
          style={r.can_manage_permissions ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
          title={r.can_manage_permissions ? undefined : t('domainSpace.noManagePermission')}
          onClick={() => r.can_manage_permissions && permRef.current?.open(r)}
        >
          <TeamOutlined style={{ fontSize: 11, marginRight: 4 }} />
          {t('domainSpace.setPermission')}
        </span>
      ),
    },
    {
      title: t('domainSpace.actions'),
      key: 'actions',
      width: 120,
      render: (_: unknown, r: DomainSpace) => (
        <Space>
          <a
            className="yx-table-action"
            style={r.can_manage_permissions ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
            title={r.can_manage_permissions ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => r.can_manage_permissions && openEdit(r)}
          >
            {t('common.edit')}
          </a>
          <a
            className="yx-table-action"
            style={
              r.can_manage_permissions
                ? { color: '#dc2626' }
                : { color: '#dc2626', opacity: 0.4, cursor: 'not-allowed' }
            }
            title={r.can_manage_permissions ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => r.can_manage_permissions && setDeleting(r)}
          >
            {t('common.delete')}
          </a>
        </Space>
      ),
    },
  ];

  // ── render ──
  if (loading && spaces.length === 0) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" icon={<ReloadOutlined />} onClick={loadSpaces}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="yx-domain-space-page">
      {/* 页面标题 */}
      <div className="yx-page-header">
        <h1 className="yx-page-title">{t('domainSpace.management')}</h1>
      </div>

      {/* 工具栏 */}
      <div className="yx-toolbar">
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
          placeholder={t('domainSpace.searchPlaceholder')}
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 280 }}
        />
        {canCreateSpace && (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('domainSpace.create')}
          </Button>
        )}
      </div>

      {/* 表格 */}
      <div className="yx-card">
        <Table
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={{
            total: filtered.length,
            pageSize: 10,
            showTotal: (total, range) => t('domainSpace.total', { total, from: range[0], to: range[1] }),
          }}
          size="middle"
          locale={{ emptyText: t('domainSpace.empty') }}
        />
      </div>

      {/* ── Create/Edit Modal（复用 SpaceFormModal） ── */}
      <SpaceFormModal
        open={formOpen}
        editing={editing}
        onClose={() => {
          setFormOpen(false);
          setEditing(null);
        }}
        onSaved={handleSaved}
      />

      {/* ── Delete Confirm Modal ── */}
      <Modal
        wrapClassName="yx-domain-space-modal"
        title={
          <span>
            <WarningOutlined style={{ color: '#ef4444', marginRight: 8 }} />
            {t('domainSpace.confirmDelete')}
          </span>
        }
        open={!!deleting}
        onCancel={() => setDeleting(null)}
        footer={
          <div style={{ display: 'flex', justifyContent: 'center', gap: 12 }}>
            <Button onClick={() => setDeleting(null)}>{t('common.cancel')}</Button>
            <Button danger type="primary" loading={submitting} onClick={handleDelete}>
              {t('domainSpace.confirmDelete')}
            </Button>
          </div>
        }
        width={420}
      >
        <div style={{ textAlign: 'center', padding: '12px 0' }}>
          <DeleteOutlined style={{ fontSize: 48, color: '#ef4444', marginBottom: 16, display: 'block' }} />
          <p style={{ fontSize: 16, color: '#1e293b', fontWeight: 500 }}>
            {t('domainSpace.confirmDeleteMessage', { name: deleting?.name || '' })}
          </p>
          <p style={{ fontSize: 13, color: '#94a3b8', marginTop: 8 }}>{t('domainSpace.deleteWarning')}</p>
        </div>
      </Modal>

      <SpacePermissionModal ref={permRef} onSaved={handlePermSaved} />
    </div>
  );
}
