import React, { useState, useEffect, useCallback } from 'react';
import { Input, Button, Table, Tag, Modal, Space, message, Result, Spin, Select } from 'antd';
import {
  PlusOutlined,
  SearchOutlined,
  ReloadOutlined,
  KeyOutlined,
  DeleteOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useStore } from '@/store';
import { SPACE_URL_PARAM } from '@jonex/shell-sdk';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import type { ColumnsType } from 'antd/es/table';
import {
  listServices,
  createService,
  updateService,
  deleteService,
  listServiceApiKeys,
  createServiceApiKey,
  deleteServiceApiKey,
} from '../../api/domainService';
import {
  getServiceStatusMap,
  type DomainServiceFormData,
  type DomainServiceItem,
  type KnowledgeBaseOption,
  type ServiceApiKeyItem,
} from '../../types/domainService';
import { getDomainKnowledgeList } from '../../api/domainKnowledge';
import ServiceConfigModal from '@/pages/DomainManagement/ServiceConfigModal';
import ServiceFormModal from '@/pages/DomainManagement/ServiceFormModal';
import type { ServiceFormValues } from '@/pages/DomainManagement/ServiceFormModal';

const DomainManagementServices = function DomainManagementServices() {
  const { t } = useTranslation();
  const { global } = useStore();

  // 空间管理权限（owner/租户管理员）：服务增删改、启停、配置、API key 均为管理动作
  const canManageServices = global.currentSpace?.can_write_space ?? false;
  const [searchParams, setSearchParams] = useSearchParams();

  // ── Data state ──
  const [services, setServices] = useState<DomainServiceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [submitting, setSubmitting] = useState(false);

  const [serviceFormOpen, setServiceFormOpen] = useState(false);
  const [editingService, setEditingService] = useState<DomainServiceItem | null>(null);
  const [submittingService, setSubmittingService] = useState(false);

  // ── Modal state ──
  const [deleteTarget, setDeleteTarget] = useState<DomainServiceItem | null>(null);

  // Service Config modal (API Keys)
  const [srvConfigOpen, setSrvConfigOpen] = useState(false);
  const [srvConfigTarget, setSrvConfigTarget] = useState<DomainServiceItem | null>(null);
  const [apiKeys, setApiKeys] = useState<ServiceApiKeyItem[]>([]);
  const [apiKeysLoading, setApiKeysLoading] = useState(false);
  const [creatingKey, setCreatingKey] = useState(false);

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

  // ── 新建 / 编辑服务弹窗 ──
  const openCreateService = () => {
    setEditingService(null);
    setServiceFormOpen(true);
  };

  const openEditService = (item: DomainServiceItem) => {
    setEditingService(item);
    setServiceFormOpen(true);
  };

  const handleSubmitService = async (values: ServiceFormValues) => {
    if (!global.currentSpaceId) {
      message.warning(t('domainManagement.spaceRequired'));
      return;
    }
    setSubmittingService(true);
    try {
      const data: DomainServiceFormData = {
        name: values.name,
        space_id: global.currentSpaceId,
        domain_type: values.domain_type || undefined,
        status: values.status ? 'active' : 'inactive',
        kb_ids: values.kb_ids,
      };
      if (editingService) {
        await updateService(editingService.id, data);
      } else {
        await createService(data);
      }
      message.success(t('common.saveSuccess'));
      setServiceFormOpen(false);
      await loadServices();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSubmittingService(false);
    }
  };

  // ── Service Config modal ──
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
      render: (v: string, _r: DomainServiceItem) => {
        const cfg = getServiceStatusMap(t)[v];
        if (!cfg) return <Tag>{v}</Tag>;
        return <Tag color={v === 'active' ? 'success' : v === 'testing' ? 'warning' : 'error'}>{cfg.label}</Tag>;
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
            <a
              className="yx-table-action"
              style={canManageServices ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
              title={canManageServices ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => canManageServices && toggleServiceStatus(r)}
            >
              {isActive ? t('domainManagement.disable') : t('domainManagement.enable')}
            </a>
            <a
              className="yx-table-action"
              style={canManageServices ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
              title={canManageServices ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => canManageServices && openSrvConfig(r)}
            >
              <KeyOutlined style={{ fontSize: 11 }} /> {t('domainManagement.config')}
            </a>
            <a
              className="yx-table-action"
              style={canManageServices ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
              title={canManageServices ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => canManageServices && openEditService(r)}
            >
              {t('common.edit')}
            </a>
            <a
              className="yx-table-action"
              style={
                canManageServices ? { color: '#dc2626' } : { color: '#dc2626', opacity: 0.4, cursor: 'not-allowed' }
              }
              title={canManageServices ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => canManageServices && setDeleteTarget(r)}
            >
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
    <>
      <PageHeader title={t('domainManagementServices.title')} style={{ marginBottom: 4 }} />
      <PageContent showCard={false}>
        <div className="yx-domain-management-page">
          <p className="yx-page-desc" style={{ marginBottom: 24 }}>
            {t('domainManagementServices.description')}
          </p>

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
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!canManageServices}
                title={canManageServices ? undefined : t('domainSpace.noManagePermission')}
                onClick={() => openCreateService()}
              >
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
            open={serviceFormOpen}
            editing={editingService}
            spaceName={global.currentSpace?.name}
            availableKbs={availableKbs}
            submitting={submittingService}
            showType
            onSubmit={handleSubmitService}
            onCancel={() => setServiceFormOpen(false)}
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

          {/* ===== 服务配置（API Key / MCP 接入）===== */}
          <ServiceConfigModal
            open={srvConfigOpen}
            canManage={canManageServices}
            srvConfigTarget={srvConfigTarget}
            apiKeys={apiKeys}
            apiKeysLoading={apiKeysLoading}
            creatingKey={creatingKey}
            onCreateKey={handleCreateKey}
            onDeleteKey={handleDeleteKey}
            onCancel={() => setSrvConfigOpen(false)}
          />
        </div>
      </PageContent>
    </>
  );
};

export default DomainManagementServices;
