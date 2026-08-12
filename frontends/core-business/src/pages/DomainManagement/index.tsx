import React, { useState, useEffect, useCallback } from 'react';
import { Input, Button, Table, Tag, Modal, Space, message, Result, Spin } from 'antd';
import { PlusOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useStore } from '@/store';
import { SPACE_URL_PARAM } from '@jonex/shell-sdk';
import type { ColumnsType } from 'antd/es/table';
import {
  listServices,
  createService,
  updateService,
  deleteService,
  listServiceApiKeys,
  createServiceApiKey,
  deleteServiceApiKey,
  getServicePermissions,
  setServicePermissions,
} from '../../api/domainService';
import {
  getServiceStatusMap,
  userToPermMember,
  type DomainServiceItem,
  type DomainServiceFormData,
  type KnowledgeBaseOption,
  type PermMember,
  type ServiceApiKeyItem,
} from '../../types/domainService';
import { listUsers, type PlatformUser } from '../../api/user';
import { getDomainKnowledgeList } from '../../api/domainKnowledge';
import DomainFormModal from './DomainFormModal';
import DeleteConfirmModal from './DeleteConfirmModal';
import PermissionModal from './PermissionModal';
import ServiceConfigModal from './ServiceConfigModal';
import './index.scss';

const DomainManagement = function DomainManagement() {
  const { t } = useTranslation();
  const { global } = useStore();
  const [searchParams, setSearchParams] = useSearchParams();
  // ── Data state ──
  const [services, setServices] = useState<DomainServiceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // ── Modal state ──
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<DomainServiceItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DomainServiceItem | null>(null);

  // Permission modal
  const [permOpen, setPermOpen] = useState(false);
  const [permTarget, setPermTarget] = useState<DomainServiceItem | null>(null);
  const [permSearch, setPermSearch] = useState('');
  const [permMembers, setPermMembers] = useState<PermMember[]>([]);
  const [permLoading, setPermLoading] = useState(false);
  const [permSaving, setPermSaving] = useState(false);

  // Service Config modal (multi-key)
  const [srvConfigOpen, setSrvConfigOpen] = useState(false);
  const [srvConfigTarget, setSrvConfigTarget] = useState<DomainServiceItem | null>(null);
  const [apiKeys, setApiKeys] = useState<ServiceApiKeyItem[]>([]);
  const [apiKeysLoading, setApiKeysLoading] = useState(false);
  const [creatingKey, setCreatingKey] = useState(false);

  const spaceMap = new Map(global.spaces.map((s) => [s.id, s.name]));
  // 从服务列表中构建 kb_id → kb_name 映射
  const kbNameMap = new Map<string, string>();
  services.forEach((s) => {
    s.kb_ids?.forEach((kid, i) => {
      if (!kbNameMap.has(kid) && s.kb_names?.[i]) {
        kbNameMap.set(kid, s.kb_names[i]);
      }
    });
  });

  // ── Data loading ──
  const loadServices = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listServices(global.currentSpaceId || undefined, 0, 100);
      setServices(result.items);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [global.currentSpaceId, t]);

  useEffect(() => {
    global.loadSpaces();
  }, []);

  // URL sync: 挂载时 URL 优先覆盖 store
  useEffect(() => {
    const urlSpaceId = searchParams.get(SPACE_URL_PARAM);
    if (urlSpaceId && global.spaces.some((s) => s.id === urlSpaceId)) {
      global.setCurrentSpaceId(urlSpaceId, { persist: true, broadcast: false });
    }
  }, []);

  // store 变化 → 加载 + URL
  useEffect(() => {
    if (global.spacesLoaded) {
      loadServices();
      const urlSpaceId = searchParams.get(SPACE_URL_PARAM);
      if (global.currentSpaceId && global.currentSpaceId !== urlSpaceId) {
        setSearchParams(
          (prev) => {
            const next = new URLSearchParams(prev);
            next.set(SPACE_URL_PARAM, global.currentSpaceId!);
            return next;
          },
          { replace: true },
        );
      }
    }
  }, [global.currentSpaceId, global.spacesLoaded]);

  // ── Filtering（仅搜索关键字做前端筛选，空间已走服务端） ──
  const filtered = services.filter((s) => {
    if (search && !s.name.includes(search) && !(s.description || '').includes(search)) return false;
    return true;
  });

  // ── CRUD handlers ──
  const openCreate = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (item: DomainServiceItem) => {
    setEditing(item);
    setFormOpen(true);
  };

  const handleSave = async (values: { name: string; kb_ids: string[]; status: boolean }) => {
    if (!global.currentSpaceId) {
      message.warning(t('domainManagement.spaceRequired'));
      return;
    }
    setSubmitting(true);
    try {
      const data: DomainServiceFormData = {
        name: values.name.trim(),
        space_id: global.currentSpaceId!,
        status: values.status ? 'active' : 'inactive',
        kb_ids: values.kb_ids,
      };
      if (editing) {
        await updateService(editing.id, data);
        message.success(t('common.saveSuccess'));
      } else {
        await createService(data);
        message.success(t('common.saveSuccess'));
      }
      setFormOpen(false);
      await loadServices();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setSubmitting(true);
    try {
      await deleteService(deleteTarget.id);
      message.success(t('common.deleteSuccess'));
      setDeleteTarget(null);
      await loadServices();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.deleteFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleServiceStatus = async (item: DomainServiceItem) => {
    const newStatus = item.status === 'active' ? 'inactive' : 'active';
    try {
      await updateService(item.id, { status: newStatus });
      setServices((prev) => prev.map((s) => (s.id === item.id ? { ...s, status: newStatus } : s)));
      message.success(newStatus === 'active' ? t('status.active') : t('status.inactive'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.operationFailed'));
    }
  };

  // ── Permission modal ──
  const openPermModal = async (item: DomainServiceItem) => {
    setPermTarget(item);
    setPermSearch('');
    setPermOpen(true);
    setPermLoading(true);
    try {
      const result = await getServicePermissions(item.id);
      const perms = result?.permissions ?? [];
      if (Array.isArray(perms) && perms.length > 0) {
        // 尝试加载用户列表以解析名称
        let userMap: Map<string, PlatformUser> = new Map();
        try {
          const userResult = await listUsers(1, 100);
          for (const u of userResult.items) {
            userMap.set(String(u.id), u);
          }
        } catch {
          /* 用户列表加载失败不影响权限展示 */
        }
        const members: PermMember[] = perms.map((p) => {
          const uid = String(p.user_id);
          const user = userMap.get(uid);
          return user
            ? userToPermMember(user, p.role === 'manager' ? 'manager' : 'viewer')
            : {
                id: uid,
                name: t('domainManagement.userPrefix', { id: uid.slice(0, 8) }),
                department: '',
                avatar: uid.charAt(0).toUpperCase(),
                avatarColor: '#94a3b8',
                role: (p.role === 'manager' ? 'manager' : 'viewer') as 'viewer' | 'manager',
              };
        });
        setPermMembers(members);
      } else {
        setPermMembers([]);
      }
    } catch {
      setPermMembers([]);
    } finally {
      setPermLoading(false);
    }
  };

  const handlePermSave = async () => {
    if (!permTarget) return;
    setPermSaving(true);
    try {
      const permissions = permMembers.map((m) => ({
        user_id: m.id,
        role: m.role,
      }));
      await setServicePermissions(permTarget.id, permissions);
      message.success(t('common.saveSuccess'));
      setPermOpen(false);
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setPermSaving(false);
    }
  };

  const addPermMember = (user: PlatformUser) => {
    setPermMembers((prev) => {
      if (prev.some((m) => m.id === String(user.id))) return prev;
      return [...prev, userToPermMember(user, 'viewer')];
    });
  };

  const removePermMember = (userId: string) => {
    setPermMembers((prev) => prev.filter((m) => m.id !== userId));
  };

  // ── Service Config modal (multi-key) ──
  const openSrvConfig = async (item: DomainServiceItem) => {
    setSrvConfigTarget(item);
    setSrvConfigOpen(true);
    setApiKeysLoading(true);
    try {
      const result = await listServiceApiKeys(item.id);
      setApiKeys(result.items || []);
    } catch {
      setApiKeys([]);
    } finally {
      setApiKeysLoading(false);
    }
  };

  const handleCreateKey = async () => {
    if (!srvConfigTarget) return;
    setCreatingKey(true);
    try {
      const newKey = await createServiceApiKey(srvConfigTarget.id, {
        expires_in_days: 365,
      });
      setApiKeys((prev) => [newKey, ...prev]);
      message.success(t('common.apiKeyCreated'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setCreatingKey(false);
    }
  };

  const handleDeleteKey = async (keyId: string) => {
    if (!srvConfigTarget) return;
    Modal.confirm({
      title: t('common.confirmDeleteApiKey'),
      content: t('common.apiKeyDeleteWarning'),
      okText: t('common.okText'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        await deleteServiceApiKey(srvConfigTarget.id, keyId);
        setApiKeys((prev) => prev.filter((k) => k.id !== keyId));
        message.success(t('common.apiKeyDeleted'));
      },
    });
  };

  // ── Knowledge base helpers ──
  const [availableKbs, setAvailableKbs] = useState<KnowledgeBaseOption[]>([]);

  useEffect(() => {
    if (!global.currentSpaceId) return;
    // 尝试从领域知识库 API 加载真实 KB 列表，失败则使用 MOCK 数据
    getDomainKnowledgeList({
      page: 1,
      pageSize: 100,
      spaceId: global.currentSpaceId,
    })
      .then((result) => {
        if (result.list && result.list.length > 0) {
          setAvailableKbs(result.list.map((kb) => ({ id: kb.id, name: kb.name })));
        }
      })
      .catch(() => {
        /* fallback to MOCK */
      });
  }, [global.currentSpaceId]);

  // ── Table columns ──
  const columns: ColumnsType<DomainServiceItem> = [
    {
      title: t('domainManagement.name'),
      dataIndex: 'name',
      key: 'name',
      width: 140,
      render: (v: string) => (
        <span
          className="yx-domain-name"
          onClick={() => {
            /* 可导航到领域详情 */
          }}
        >
          {v}
        </span>
      ),
    },
    {
      title: t('domainManagement.space'),
      dataIndex: 'space_id',
      key: 'space',
      width: 140,
      render: (id: string) => spaceMap.get(id) || id,
    },
    {
      title: t('domainManagement.kb'),
      key: 'kbs',
      width: 240,
      render: (_: unknown, r: DomainServiceItem) => (
        <div className="yx-kb-tags">
          {r.kb_ids && r.kb_ids.length > 0 ? (
            r.kb_ids.map((kbId) => (
              <span key={kbId} className="yx-kb-tag">
                {kbNameMap.get(kbId) || kbId}
              </span>
            ))
          ) : (
            <span className="yx-kb-tag">—</span>
          )}
        </div>
      ),
    },
    // 权限当前先注释
    // {
    //   title: '权限设置',
    //   key: 'perm',
    //   width: 110,
    //   render: (_: unknown, r: DomainServiceItem) => (
    //     <span className="yx-perm-badge" onClick={() => openPermModal(r)}>
    //       <TeamOutlined style={{ fontSize: 11 }} />
    //       设置权限
    //     </span>
    //   ),
    // },
    {
      title: t('domainManagement.status'),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string, r: DomainServiceItem) => {
        const cfg = getServiceStatusMap(t)[v];
        if (!cfg) return <Tag>{v}</Tag>;
        return (
          <Tag color={v === 'active' ? 'success' : v === 'testing' ? 'warning' : 'error'}>{cfg.label}</Tag>
        );
      },
    },
    {
      title: t('domainManagement.actions'),
      key: 'actions',
      width: 240,
      render: (_: unknown, r: DomainServiceItem) => {
        const isActive = r.status === 'active';
        return (
          <Space size={4}>
            <Button type="link" size="small" onClick={() => openSrvConfig(r)}>
              {t('domainManagement.srvConfigTitle')}
            </Button>
            <Button type="link" size="small" onClick={() => toggleServiceStatus(r)}>
              {isActive ? t('domainManagement.disable') : t('domainManagement.enable')}
            </Button>
            <Button type="link" size="small" onClick={() => openEdit(r)}>
              {t('common.edit')}
            </Button>
            <Button type="link" size="small" danger onClick={() => setDeleteTarget(r)}>
              {t('common.delete')}
            </Button>
          </Space>
        );
      },
    },
  ];

  // ── Render ──
  if (loading && services.length === 0) {
    return (
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          minHeight: 300,
        }}
      >
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
          <Button type="primary" icon={<ReloadOutlined />} onClick={() => loadServices()}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="yx-domain-management-page">
      {/* Page Header */}
      <div className="yx-page-header">
        <h1 className="yx-page-title">{t('domainManagement.title')}</h1>
        <p className="yx-page-desc">{t('domainManagement.description')}</p>
      </div>

      {/* Card */}
      <div className="yx-card">
        <div className="yx-toolbar">
          <Input
            prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
            placeholder={t('domainManagement.search')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 280 }}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('domainManagement.create')}
          </Button>
        </div>

        <Table<DomainServiceItem>
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={{
            total: filtered.length,
            pageSize: 10,
            showTotal: (total, range) =>
              t('common.totalItemsRange', {
                total,
                from: range[0],
                to: range[1],
              }),
          }}
          size="middle"
          locale={{ emptyText: t('domainManagement.empty') }}
        />
      </div>

      {/* Create/Edit Modal */}
      <DomainFormModal
        open={formOpen}
        editing={editing}
        availableKbs={availableKbs}
        submitting={submitting}
        onSave={handleSave}
        onCancel={() => setFormOpen(false)}
      />

      {/* Delete Confirm Modal */}
      <DeleteConfirmModal
        open={!!deleteTarget}
        deleteTarget={deleteTarget}
        submitting={submitting}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
      />

      {/* Permission Modal */}
      <PermissionModal
        open={permOpen}
        permTarget={permTarget}
        permMembers={permMembers}
        permSearch={permSearch}
        permLoading={permLoading}
        permSaving={permSaving}
        onPermSearchChange={setPermSearch}
        onRoleChange={(userId, role) => {
          setPermMembers((prev) => prev.map((m) => (m.id === userId ? { ...m, role } : m)));
        }}
        onRemoveMember={removePermMember}
        onAddMember={addPermMember}
        onSave={handlePermSave}
        onCancel={() => {
          setPermOpen(false);
        }}
      />

      {/* Service Config Modal */}
      <ServiceConfigModal
        open={srvConfigOpen}
        srvConfigTarget={srvConfigTarget}
        apiKeys={apiKeys}
        apiKeysLoading={apiKeysLoading}
        creatingKey={creatingKey}
        onCreateKey={handleCreateKey}
        onDeleteKey={handleDeleteKey}
        onCancel={() => setSrvConfigOpen(false)}
      />
    </div>
  );
};

export default DomainManagement;
