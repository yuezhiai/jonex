import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Input, Button, Table, Tag, Modal, Space, message, Result, Spin, Select } from 'antd';
import {
  PlusOutlined,
  SearchOutlined,
  ReloadOutlined,
  TeamOutlined,
  KeyOutlined,
  DeleteOutlined,
  CopyOutlined,
  CheckOutlined,
  StopOutlined,
  CloseOutlined,
  UserAddOutlined,
} from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useStore } from '@/store';
import { SPACE_URL_PARAM } from '@jonex/shell-sdk';
import type { ColumnsType } from 'antd/es/table';
import {
  listServices,
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
  type KnowledgeBaseOption,
  type PermMember,
  type ServiceApiKeyItem,
} from '../../types/domainService';
import { listUsers, type PlatformUser } from '../../api/user';
import { getDomainKnowledgeList } from '../../api/domainKnowledge';
import ServiceFormModal from './ServiceFormModal';
import type { ServiceFormModalHandle } from './ServiceFormModal';

const DomainManagementServices = function DomainManagementServices() {
  const { t } = useTranslation();
  const { global } = useStore();
  const [searchParams, setSearchParams] = useSearchParams();

  // ── Data state ──
  const [services, setServices] = useState<DomainServiceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [submitting, setSubmitting] = useState(false);

  const serviceFormRef = useRef<ServiceFormModalHandle>(null);

  // ── Modal state ──
  const [deleteTarget, setDeleteTarget] = useState<DomainServiceItem | null>(null);

  // Permission modal
  const [permOpen, setPermOpen] = useState(false);
  const [permTarget, setPermTarget] = useState<DomainServiceItem | null>(null);
  const [permSearch, setPermSearch] = useState('');
  const [permMembers, setPermMembers] = useState<PermMember[]>([]);
  const [permLoading, setPermLoading] = useState(false);
  const [permSaving, setPermSaving] = useState(false);

  // User selector for permission modal
  const [userSelectOpen, setUserSelectOpen] = useState(false);
  const [userSearchText, setUserSearchText] = useState('');
  const [availableUsers, setAvailableUsers] = useState<PlatformUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const userSelectRef = useRef<HTMLDivElement>(null);

  // Service Config modal (API Keys)
  const [srvConfigOpen, setSrvConfigOpen] = useState(false);
  const [srvConfigTarget, setSrvConfigTarget] = useState<DomainServiceItem | null>(null);
  const [apiKeys, setApiKeys] = useState<ServiceApiKeyItem[]>([]);
  const [apiKeysLoading, setApiKeysLoading] = useState(false);
  const [creatingKey, setCreatingKey] = useState(false);
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);

  // Knowledge base list for form
  const [availableKbs, setAvailableKbs] = useState<KnowledgeBaseOption[]>([]);

  const spaceMap = new Map(global.spaces.map((s) => [s.id, s.name]));
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

  // URL sync
  useEffect(() => {
    const urlSpaceId = searchParams.get(SPACE_URL_PARAM);
    if (urlSpaceId && global.spaces.some((s) => s.id === urlSpaceId)) {
      global.setCurrentSpaceId(urlSpaceId, { persist: true, broadcast: false });
    }
  }, []);

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

  // Click outside to close user selector
  useEffect(() => {
    if (!userSelectOpen) return;
    const handler = (e: MouseEvent) => {
      if (userSelectRef.current && !userSelectRef.current.contains(e.target as Node)) {
        setUserSelectOpen(false);
        setUserSearchText('');
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [userSelectOpen]);

  // Load KB list for form
  useEffect(() => {
    getDomainKnowledgeList({ page: 1, pageSize: 100, spaceId: global.currentSpaceId || undefined })
      .then((result) => {
        if (result.list && result.list.length > 0) {
          setAvailableKbs(result.list.map((kb) => ({ id: kb.id, name: kb.name })));
        }
      })
      .catch(() => {
        /* fallback to empty */
      });
  }, [global.currentSpaceId]);

  // ── Type filter options ──
  const typeOptions = [
    { value: 'all', label: t('domainManagementServices.filterAllTypes') },
    { value: 'retrieval', label: t('domainManagementServices.typeRetrieval') },
    { value: 'inference', label: t('domainManagementServices.typeInference') },
    { value: 'analysis', label: t('domainManagementServices.typeAnalysis') },
    { value: 'general', label: t('domainManagementServices.typeGeneral') },
  ];

  // ── Filtering ──
  const filtered = services.filter((s) => {
    if (search && !s.name.includes(search) && !(s.description || '').includes(search)) return false;
    if (typeFilter !== 'all') {
      const dt = s.domain_type || 'general';
      if (dt !== typeFilter) return false;
    }
    return true;
  });

  // ── CRUD handlers ──
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
    setUserSelectOpen(false);
    setUserSearchText('');
    try {
      const result = await getServicePermissions(item.id);
      const perms = result?.permissions ?? [];
      if (Array.isArray(perms) && perms.length > 0) {
        let userMap: Map<string, PlatformUser> = new Map();
        try {
          const userResult = await listUsers(1, 100);
          for (const u of userResult.items) {
            userMap.set(String(u.id), u);
          }
        } catch {
          /* user list failure is ok */
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

  const loadAvailableUsers = useCallback(async () => {
    setUsersLoading(true);
    try {
      const result = await listUsers(1, 100);
      setAvailableUsers(result.items);
    } catch {
      setAvailableUsers([]);
    } finally {
      setUsersLoading(false);
    }
  }, []);

  const addPermMember = (user: PlatformUser) => {
    setPermMembers((prev) => {
      if (prev.some((m) => m.id === String(user.id))) return prev;
      return [...prev, userToPermMember(user, 'viewer')];
    });
  };

  const removePermMember = (userId: string) => {
    setPermMembers((prev) => prev.filter((m) => m.id !== userId));
  };

  const filteredPermMembers = permMembers.filter((m) => {
    if (!permSearch) return true;
    return m.name.includes(permSearch) || m.department.includes(permSearch);
  });

  const addedUserIds = new Set(permMembers.map((m) => m.id));
  const filteredAvailableUsers = availableUsers.filter((u) => {
    if (!userSearchText) return !addedUserIds.has(String(u.id));
    const q = userSearchText.toLowerCase();
    return (
      !addedUserIds.has(String(u.id)) &&
      ((u.display_name || '').toLowerCase().includes(q) ||
        u.username.toLowerCase().includes(q) ||
        (u.email || '').toLowerCase().includes(q))
    );
  });

  // ── Service Config modal ──
  const openSrvConfig = async (item: DomainServiceItem) => {
    setSrvConfigTarget(item);
    setCopiedKeyId(null);
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

  const handleCopyKey = async (keyId: string, key: string) => {
    try {
      await navigator.clipboard.writeText(key);
      setCopiedKeyId(keyId);
      setTimeout(() => setCopiedKeyId(null), 2000);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = key;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopiedKeyId(keyId);
      setTimeout(() => setCopiedKeyId(null), 2000);
    }
  };

  const handleCreateKey = async () => {
    if (!srvConfigTarget) return;
    setCreatingKey(true);
    try {
      const newKey = await createServiceApiKey(srvConfigTarget.id, { expires_in_days: 365 });
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

  const formatDate = (dateStr: string | null): string => {
    if (!dateStr) return '—';
    try {
      return new Date(dateStr).toISOString().slice(0, 10);
    } catch {
      return dateStr;
    }
  };

  const typeLabel = (v: string | null): string => {
    const key = v || 'general';
    if (key === 'retrieval') return t('domainManagementServices.typeRetrieval');
    if (key === 'inference') return t('domainManagementServices.typeInference');
    if (key === 'analysis') return t('domainManagementServices.typeAnalysis');
    if (key === 'general') return t('domainManagementServices.typeGeneral');
    return key;
  };

  // ── Table columns ──
  const columns: ColumnsType<DomainServiceItem> = [
    {
      title: t('domainManagementServices.columnName'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
    },
    {
      title: t('domainManagementServices.columnDomain'),
      dataIndex: 'space_id',
      key: 'space',
      width: 140,
      render: (id: string) => spaceMap.get(id) || id,
    },
    {
      title: t('domainManagementServices.columnType'),
      dataIndex: 'domain_type',
      key: 'domain_type',
      width: 110,
      render: (v: string | null) => (
        <Tag color={v === 'inference' ? 'blue' : v === 'retrieval' ? 'green' : v === 'analysis' ? 'purple' : 'default'}>
          {typeLabel(v)}
        </Tag>
      ),
    },
    {
      title: t('domainManagement.kb'),
      key: 'kbs',
      width: 200,
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
    {
      title: t('domainManagementServices.columnStatus'),
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
      title: t('domainManagementServices.columnActions'),
      key: 'actions',
      width: 280,
      render: (_: unknown, r: DomainServiceItem) => {
        const isActive = r.status === 'active';
        return (
          <Space>
            <a className="yx-table-action" onClick={() => toggleServiceStatus(r)}>
              {isActive ? t('domainManagement.disable') : t('domainManagement.enable')}
            </a>
            <a className="yx-table-action" onClick={() => openPermModal(r)}>
              <TeamOutlined style={{ fontSize: 11 }} /> {t('domainManagement.perm')}
            </a>
            <a className="yx-table-action" onClick={() => openSrvConfig(r)}>
              <KeyOutlined style={{ fontSize: 11 }} /> {t('domainManagement.config')}
            </a>
            <a className="yx-table-action" onClick={() => serviceFormRef.current?.openEdit(r)}>
              {t('common.edit')}
            </a>
            <a className="yx-table-action" style={{ color: '#dc2626' }} onClick={() => setDeleteTarget(r)}>
              {t('common.delete')}
            </a>
          </Space>
        );
      },
    },
  ];

  // ── Render ──
  if (loading && services.length === 0) {
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
        <h1 className="yx-page-title">{t('domainManagementServices.title')}</h1>
        <p className="yx-page-desc">{t('domainManagementServices.description')}</p>
      </div>

      {/* Card */}
      <div className="yx-card">
        <div className="yx-toolbar">
          <Input
            prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
            placeholder={t('domainManagementServices.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 240 }}
          />
          <Select value={typeFilter} onChange={setTypeFilter} style={{ width: 140 }} options={typeOptions} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => serviceFormRef.current?.openCreate()}>
            {t('domainManagementServices.createService')}
          </Button>
        </div>

        <Table<DomainServiceItem>
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          pagination={{
            total: filtered.length,
            pageSize: 10,
            showTotal: (total, range) => t('common.totalItemsRange', { total, from: range[0], to: range[1] }),
          }}
          size="middle"
          locale={{ emptyText: t('domainManagement.empty') }}
        />
      </div>

      <ServiceFormModal
        ref={serviceFormRef}
        spaceId={global.currentSpaceId}
        availableKbs={availableKbs}
        onSaved={loadServices}
      />

      {/* ===== Delete Confirm Modal ===== */}
      <Modal
        wrapClassName="yx-domain-space-modal"
        title={
          <span>
            <StopOutlined style={{ color: '#ef4444', marginRight: 8 }} />
            {t('common.confirmDeleteTitle')}
          </span>
        }
        open={!!deleteTarget}
        onCancel={() => setDeleteTarget(null)}
        footer={
          <div style={{ display: 'flex', justifyContent: 'center', gap: 12 }}>
            <Button onClick={() => setDeleteTarget(null)}>{t('common.cancel')}</Button>
            <Button danger type="primary" loading={submitting} onClick={handleDelete}>
              {t('common.okText')}
            </Button>
          </div>
        }
        width={420}
      >
        <div style={{ textAlign: 'center', padding: '12px 0' }}>
          <DeleteOutlined style={{ fontSize: 48, color: '#ef4444', marginBottom: 16, display: 'block' }} />
          <p style={{ fontSize: 16, color: '#1e293b', fontWeight: 500 }}>
            {t('common.confirmDeleteContent', { name: deleteTarget?.name || '' })}
          </p>
          <p style={{ fontSize: 13, color: '#94a3b8', marginTop: 8 }}>{t('common.deleteWarning')}</p>
        </div>
      </Modal>

      {/* ===== Permission Modal ===== */}
      <Modal
        wrapClassName="yx-domain-space-modal"
        title={
          <span>
            <TeamOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
            {t('domainManagement.permTitle')}
          </span>
        }
        open={permOpen}
        onCancel={() => {
          setPermOpen(false);
          setUserSelectOpen(false);
        }}
        onOk={handlePermSave}
        confirmLoading={permSaving}
        okText={t('domainManagement.savePermission')}
        cancelText={t('common.cancel')}
        width={600}
      >
        <p style={{ fontSize: 14, color: '#475569', marginBottom: 12 }}>
          {t('domainManagement.permDesc', { name: permTarget?.name || '' })}
        </p>

        <div ref={userSelectRef} style={{ position: 'relative', marginBottom: 12 }}>
          <Button
            icon={<UserAddOutlined />}
            onClick={() => {
              const willOpen = !userSelectOpen;
              setUserSelectOpen(willOpen);
              setUserSearchText('');
              if (willOpen && availableUsers.length === 0) loadAvailableUsers();
            }}
            style={{ marginBottom: userSelectOpen ? 8 : 0 }}
          >
            {t('domainManagement.addMember')}
          </Button>
          {userSelectOpen && (
            <div
              style={{
                position: 'absolute',
                top: 38,
                left: 0,
                zIndex: 10,
                width: 320,
                background: '#fff',
                borderRadius: 8,
                boxShadow: '0 4px 20px rgba(0,0,0,.12)',
                border: '1px solid #e2e8f0',
                overflow: 'hidden',
              }}
            >
              <div style={{ padding: '8px 12px', borderBottom: '1px solid #e2e8f0' }}>
                <Input
                  size="small"
                  placeholder={t('domainManagement.searchUser')}
                  prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
                  value={userSearchText}
                  onChange={(e) => setUserSearchText(e.target.value)}
                  allowClear
                />
              </div>
              <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                {usersLoading ? (
                  <div style={{ textAlign: 'center', padding: 20 }}>
                    <Spin size="small" />
                  </div>
                ) : filteredAvailableUsers.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
                    {userSearchText ? t('domainManagement.noMatchUser') : t('domainManagement.noAvailableUser')}
                  </div>
                ) : (
                  filteredAvailableUsers.slice(0, 30).map((user) => (
                    <div
                      key={user.id}
                      className="yx-perm-user-row"
                      style={{ cursor: 'pointer', padding: '8px 12px' }}
                      onClick={() => {
                        addPermMember(user);
                        setUserSearchText('');
                      }}
                    >
                      <div className="yx-perm-avatar" style={{ background: userToPermMember(user).avatarColor }}>
                        {userToPermMember(user).avatar}
                      </div>
                      <div className="yx-perm-user-info" style={{ flex: 1 }}>
                        <div className="yx-perm-user-name">{user.display_name || user.username}</div>
                        <div className="yx-perm-user-dept">{user.email || user.role || ''}</div>
                      </div>
                      <span style={{ fontSize: 20, color: '#3b82f6', lineHeight: 1 }}>+</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>

        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
          placeholder={t('domainManagement.searchMember')}
          value={permSearch}
          onChange={(e) => setPermSearch(e.target.value)}
          style={{ width: '100%', marginBottom: 12 }}
        />

        <div style={{ maxHeight: 280, overflowY: 'auto' }}>
          {permLoading ? (
            <div style={{ textAlign: 'center', padding: 24 }}>
              <Spin />
            </div>
          ) : filteredPermMembers.length === 0 ? (
            <div style={{ textAlign: 'center', padding: 24, color: '#94a3b8', fontSize: 13 }}>
              {permSearch ? t('domainManagement.noMatchMember') : t('domainManagement.noMembers')}
            </div>
          ) : (
            filteredPermMembers.map((member) => (
              <div key={member.id} className="yx-perm-user-row">
                <div className="yx-perm-avatar" style={{ background: member.avatarColor }}>
                  {member.avatar}
                </div>
                <div className="yx-perm-user-info" style={{ flex: 1 }}>
                  <div className="yx-perm-user-name">{member.name}</div>
                  <div className="yx-perm-user-dept">{member.department || `ID: ${member.id.slice(0, 8)}`}</div>
                </div>
                <div className="yx-perm-radio">
                  <label className={`yx-perm-radio-label${member.role === 'viewer' ? ' is-checked' : ''}`}>
                    <input
                      type="radio"
                      name={`perm-${member.id}`}
                      value="viewer"
                      checked={member.role === 'viewer'}
                      onChange={() =>
                        setPermMembers((prev) =>
                          prev.map((m) => (m.id === member.id ? { ...m, role: 'viewer' as const } : m)),
                        )
                      }
                    />
                    {t('permission.view')}
                  </label>
                  <label className={`yx-perm-radio-label${member.role === 'manager' ? ' is-checked' : ''}`}>
                    <input
                      type="radio"
                      name={`perm-${member.id}`}
                      value="manager"
                      checked={member.role === 'manager'}
                      onChange={() =>
                        setPermMembers((prev) =>
                          prev.map((m) => (m.id === member.id ? { ...m, role: 'manager' as const } : m)),
                        )
                      }
                    />
                    {t('permission.manage')}
                  </label>
                </div>
                <Button
                  type="text"
                  className="yx-perm-remove-btn"
                  onClick={() => removePermMember(member.id)}
                  title={t('domainManagement.removeMember')}
                  style={{ color: '#94a3b8', fontSize: 16, padding: '0 0 0 8px', lineHeight: 1 }}
                >
                  <CloseOutlined />
                </Button>
              </div>
            ))
          )}
        </div>
      </Modal>

      {/* ===== Service Config Modal (API Keys) ===== */}
      <Modal
        wrapClassName="yx-domain-space-modal"
        title={
          <span>
            <KeyOutlined style={{ color: '#f97316', marginRight: 8 }} />
            {t('domainManagement.srvConfigTitle')}
          </span>
        }
        open={srvConfigOpen}
        onCancel={() => setSrvConfigOpen(false)}
        footer={<Button onClick={() => setSrvConfigOpen(false)}>{t('common.cancel')}</Button>}
        width={760}
      >
        <p style={{ fontSize: 14, color: '#475569', marginBottom: 16 }}>
          {t('domainManagement.srvConfigDesc', { name: srvConfigTarget?.name || '' })}
        </p>
        <div style={{ textAlign: 'right', marginBottom: 12 }}>
          <Button type="primary" size="small" icon={<PlusOutlined />} loading={creatingKey} onClick={handleCreateKey}>
            {t('domainManagement.addApiKey')}
          </Button>
        </div>
        {apiKeysLoading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : (
          <Table<ServiceApiKeyItem>
            columns={[
              {
                title: t('domainManagement.apiKey'),
                dataIndex: 'key_encrypted',
                key: 'key_encrypted',
                width: 360,
                render: (val: string, record: ServiceApiKeyItem) => (
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <span className="yx-key-text">{val || '—'}</span>
                    {val && (
                      <Button
                        type="text"
                        className={`yx-copy-btn${copiedKeyId === record.id ? ' copied' : ''}`}
                        title={copiedKeyId === record.id ? t('common.copySuccess') : t('domainManagement.copyApiKey')}
                        onClick={() => handleCopyKey(record.id, val)}
                      >
                        {copiedKeyId === record.id ? <CheckOutlined /> : <CopyOutlined />}
                      </Button>
                    )}
                  </span>
                ),
              },
              {
                title: t('domainManagement.expiresAt'),
                dataIndex: 'expires_at',
                key: 'expires_at',
                width: 120,
                render: (val: string | null) => formatDate(val),
              },
              {
                title: t('domainManagement.srvConfigActions'),
                key: 'actions',
                width: 100,
                render: (_: unknown, record: ServiceApiKeyItem) => (
                  <Button type="text" danger onClick={() => handleDeleteKey(record.id)}>
                    <DeleteOutlined /> {t('common.delete')}
                  </Button>
                ),
              },
            ]}
            dataSource={apiKeys}
            rowKey="id"
            pagination={false}
            size="small"
            locale={{ emptyText: t('common.noApiKey') }}
          />
        )}
      </Modal>
    </div>
  );
};

export default DomainManagementServices;
