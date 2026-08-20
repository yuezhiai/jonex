import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Input, Button, message, Empty, Spin, Dropdown, Tag } from 'antd';
import {
  PlusOutlined,
  SearchOutlined,
  GlobalOutlined,
  SettingOutlined,
  EditOutlined,
  DeleteOutlined,
  EllipsisOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  DatabaseOutlined,
  LineChartOutlined,
} from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useStore } from '@/store';
import { SPACE_URL_PARAM } from '@jonex/shell-sdk';
import type {
  DomainKnowledgeItem,
  DomainKnowledgePermissionMember,
  KnowledgeBaseType,
} from '@/types/domainKnowledge';
import {
  getDomainKnowledgeList,
  getDomainKnowledgePermissions,
  saveDomainKnowledgePermissions,
  createKnowledgeInfo,
  updateKnowledgeInfo,
  deleteKnowledgeInfo,
} from '@/api/domainKnowledge';
import { listAccessMethods } from '@/api/dataSource';
import { accessTypeDisplayName } from '@/utils/dataSourceDisplay';
import PermissionModal from './PermissionModal';
import CreateEditModal from './CreateEditModal';
import DeleteConfirmModal from './DeleteConfirmModal';
import './index.scss';

const PAGE_SIZE = 6;

const DomainKnowledge = function DomainKnowledge() {
  const { global } = useStore();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // ── filter state ────────────────────────────────────
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');

  // 动态获取接入方式名称映射（从 business_domain.data_access_methods）
  const [sourceTypeNames, setSourceTypeNames] = useState<Record<string, string>>({});

  const fetchSourceTypeNames = useCallback(async () => {
    try {
      const methods = await listAccessMethods();
      const map: Record<string, string> = {};
      methods.forEach((m) => {
        map[m.accessType] = m.name;
      });
      setSourceTypeNames(map);
    } catch {
      /* 获取失败时用 accessType 原文兜底 */
    }
  }, []);

  // ── list state ───────────────────────────────────────
  const [list, setList] = useState<DomainKnowledgeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  // ── permission modal state ───────────────────────────
  const [permOpen, setPermOpen] = useState(false);
  const [permLoading, setPermLoading] = useState(false);
  const [permSaving, setPermSaving] = useState(false);
  const [currentKb, setCurrentKb] = useState<DomainKnowledgeItem | null>(null);
  const [permissionMembers, setPermissionMembers] = useState<DomainKnowledgePermissionMember[]>([]);
  const [permissionKeyword, setPermissionKeyword] = useState('');

  // ── create modal state ────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);

  // ── edit & delete state ───────────────────────────────
  const [editingKb, setEditingKb] = useState<DomainKnowledgeItem | null>(null);
  const [deletingKb, setDeletingKb] = useState<DomainKnowledgeItem | null>(null);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  // ── URL sync ────────────────────────────────────────
  // 页面挂载时 URL 优先覆盖 store
  useEffect(() => {
    const urlSpaceId = searchParams.get(SPACE_URL_PARAM);
    if (urlSpaceId && global.spaces.some((s) => s.id === urlSpaceId)) {
      global.setCurrentSpaceId(urlSpaceId, { persist: true, broadcast: false });
    }
  }, []);

  // store 变化时写回 URL
  useEffect(() => {
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
  }, [global.currentSpaceId]);

  // ── fetch helpers ────────────────────────────────────
  const fetchList = useCallback(
    async (p: number, kw: string) => {
      setLoading(true);
      try {
        const result = await getDomainKnowledgeList({
          page: p,
          pageSize: PAGE_SIZE,
          keyword: kw || undefined,
          spaceId: global.currentSpaceId || undefined,
        });
        setList(result.list);
        setTotal(result.pagination.total);
      } catch (err: any) {
        message.error(err?.message || t('domainKnowledge.fetchListFailed'));
      } finally {
        setLoading(false);
      }
    },
    [global.currentSpaceId],
  );

  // ── initial load ─────────────────────────────────────
  useEffect(() => {
    global.loadSpaces();
    fetchSourceTypeNames();
  }, []);

  // ── debounced keyword input → keyword + 回到第 1 页 ─────
  // 关键词变化时把关键词与页码一起更新（React 18 自动批处理为一次渲染）。
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setKeyword(keywordInput);
      setPage(1);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [keywordInput]);

  // ── 统一的列表请求触发器 ────────────────────────────────
  // 由 spaces 就绪、当前空间、页码、关键词共同驱动，任一变化都发起请求。
  // 此前拆成多个 effect，且分页 effect 用 `page !== 1` 守卫，导致从第 2 页
  // 返回第 1 页时不发请求。合并为单一入口后，回到第 1 页也会正常拉取。
  useEffect(() => {
    if (global.spacesLoaded) {
      fetchList(page, keyword);
    }
  }, [global.spacesLoaded, global.currentSpaceId, page, keyword]);

  // ── create handler ───────────────────────────────────
  const handleCreate = async (values: {
    name: string;
    description?: string;
    kb_type?: KnowledgeBaseType;
  }) => {
    setCreateSubmitting(true);
    try {
      const data = {
        name: values.name.trim(),
        space_id: global.currentSpaceId!,
        description: values.description?.trim() || undefined,
      };
      if (editingKb) {
        // [jonex] 编辑时绝不能带 kb_type：类型创建后不可变，后端会抛 err.kb.kb_type_immutable
        await updateKnowledgeInfo(editingKb.id, data);
        message.success(t('domainKnowledge.knowledgeBaseUpdated'));
      } else {
        await createKnowledgeInfo({ ...data, kb_type: values.kb_type ?? 'lightrag' });
        message.success(t('domainKnowledge.knowledgeBaseCreated'));
      }
      setCreateOpen(false);
      setEditingKb(null);
      fetchList(1, keyword);
    } catch (err: any) {
      message.error(
        err?.message || (editingKb ? t('domainKnowledge.updateFailed') : t('domainKnowledge.createFailed')),
      );
    } finally {
      setCreateSubmitting(false);
    }
  };

  const openCreateModal = () => {
    setEditingKb(null);
    setCreateOpen(true);
  };

  const openEditModal = (kb: DomainKnowledgeItem) => {
    setEditingKb(kb);
    setCreateOpen(true);
  };

  const handleDelete = async () => {
    if (!deletingKb) return;
    setDeleteSubmitting(true);
    try {
      await deleteKnowledgeInfo(deletingKb.id);
      message.success(t('common.deleteSuccess'));
      setDeletingKb(null);
      fetchList(1, keyword);
    } catch (err: any) {
      message.error(err?.message || t('common.deleteFailed'));
    } finally {
      setDeleteSubmitting(false);
    }
  };

  // ── permission modal ─────────────────────────────────
  const openPermModal = async (kb: DomainKnowledgeItem) => {
    setCurrentKb(kb);
    setPermissionKeyword('');
    setPermOpen(true);
    setPermLoading(true);
    try {
      const data = await getDomainKnowledgePermissions(kb.id);
      setPermissionMembers(data.members);
    } catch {
      message.error(t('domainKnowledge.fetchPermissionMembersFailed'));
    } finally {
      setPermLoading(false);
    }
  };

  const debouncedPermSearch = useCallback(
    (kw: string) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(async () => {
        if (!currentKb) return;
        setPermLoading(true);
        try {
          const data = await getDomainKnowledgePermissions(currentKb.id, kw || undefined);
          setPermissionMembers(data.members);
        } catch {
          // silent
        } finally {
          setPermLoading(false);
        }
      }, 300);
    },
    [currentKb],
  );

  const handlePermKeywordChange = (val: string) => {
    setPermissionKeyword(val);
    debouncedPermSearch(val);
  };

  const handlePermRoleChange = (userId: string, role: 'viewer' | 'editor') => {
    setPermissionMembers((prev) => prev.map((m) => (m.userId === userId ? { ...m, role } : m)));
  };

  const handleSavePermissions = async () => {
    if (!currentKb) return;
    setPermSaving(true);
    try {
      await saveDomainKnowledgePermissions(currentKb.id, {
        members: permissionMembers.map((m) => ({
          userId: m.userId,
          role: m.role,
        })),
      });
      message.success(t('domainKnowledge.savePermissionSuccess'));
      setPermOpen(false);
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.savePermissionFailed'));
    } finally {
      setPermSaving(false);
    }
  };

  // ── pagination helpers ───────────────────────────────
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const pageNumbers = (): number[] => {
    const pages: number[] = [];
    for (let i = 1; i <= totalPages; i++) pages.push(i);
    return pages;
  };

  // ── display list ──
  const displayList = list.map((item) => ({
    ...item,
    spaceName: global.currentSpace?.name || item.spaceName || '',
  }));

  // ── data source type display name ──
  const getSourceTypeDisplay = (type: string): string => {
    const localized = accessTypeDisplayName(type, t);
    return localized === type ? sourceTypeNames[type] || type : localized;
  };

  // ── render ───────────────────────────────────────────
  return (
    <div>
      {/* Page Header */}
      <div className="yx-page-title">
        <h1>{t('navigation.domainKnowledge')}</h1>
        <p className="yx-page-subtitle">{t('domainKnowledge.pageSubtitle')}</p>
      </div>

      {/* Filter Row */}
      <div className="yx-filter-row">
        <label>
          <GlobalOutlined style={{ color: '#3b82f6' }} /> {t('domainKnowledge.currentSpace')}
        </label>
        <span style={{ fontWeight: 500, color: '#0b2b5c' }}>
          {global.currentSpace?.name || t('domainKnowledge.notSelected')}
        </span>
        <span className="yx-filter-count">{t('domainKnowledge.knowledgeBaseCount', { total })}</span>
      </div>

      {/* Toolbar + Card Grid */}
      <div className="yx-page-card">
        <div className="yx-toolbar">
          <Input
            prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
            placeholder={t('domainKnowledge.searchPlaceholder')}
            value={keywordInput}
            onChange={(e) => {
              setKeywordInput(e.target.value);
            }}
            style={{ width: 280, lineHeight: 'normal' }}
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!global.currentSpaceId || !global.currentSpace?.can_write_space}
            title={global.currentSpaceId ? undefined : t('domainKnowledge.notSelected')}
            onClick={openCreateModal}
          >
            {t('domainKnowledge.newKnowledgeBase')}
          </Button>
        </div>

        {/* Loading State */}
        {loading && list.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '80px 0' }}>
            <Spin size="large" />
          </div>
        ) : list.length === 0 ? (
          /* Empty State */
          <div style={{ textAlign: 'center', padding: '80px 0' }}>
            <Empty description={t('domainKnowledge.emptyDescription')}>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!global.currentSpaceId || !global.currentSpace?.can_write_space}
                title={global.currentSpaceId ? undefined : t('domainKnowledge.notSelected')}
                onClick={openCreateModal}
              >
                {t('domainKnowledge.newKnowledgeBase')}
              </Button>
            </Empty>
          </div>
        ) : (
          /* Card Grid */
          <div className="kb-card-grid">
            {displayList.map((item) => (
              <div className="kb-card" key={item.id}>
                {/* Card Top */}
                <div className="kb-card-top">
                  <div className="kb-card-icon">
                    <DatabaseOutlined />
                  </div>
                  <div className="kb-card-info">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                      <div className="kb-card-name" onClick={() => navigate(`/domain-knowledge/${item.id}`)}>
                        {item.name}
                      </div>
                      {/* [jonex] 知识库类型标签：两种类型行为差别大（引用溯源/编译结果页），需可一眼区分 */}
                      <Tag
                        color={item.kbType === 'openkb' ? 'purple' : 'blue'}
                        style={{ marginInlineEnd: 0, flexShrink: 0, fontSize: 12 }}
                      >
                        {item.kbType === 'openkb'
                          ? t('domainKnowledge.kbTypeOpenkb')
                          : t('domainKnowledge.kbTypeLightrag')}
                      </Tag>
                    </div>
                    <div className="kb-card-meta">
                      <span>
                        <FileTextOutlined />{' '}
                        {t('domainKnowledge.docsWithCount', {
                          count: item.documentCount ?? 0,
                        })}
                      </span>
                      <span>
                        <ClockCircleOutlined /> {item.updatedAt || '—'}
                      </span>
                    </div>
                  </div>
                  {/* More Button + Dropdown */}
                  <Dropdown
                    menu={{
                      style: {
                        boxShadow:
                          '0 6px 16px 0 rgba(0,0,0,0.08), 0 3px 6px -4px rgba(0,0,0,0.12), 0 9px 28px 8px rgba(0,0,0,0.05)',
                        borderRadius: 8,
                      },
                      items: [
                        {
                          key: 'settings',
                          icon: <SettingOutlined />,
                          label: t('domainKnowledge.knowledgeBaseSettings'),
                          onClick: () => navigate(`/domain-knowledge/${item.id}/detail`),
                        },
                        {
                          key: 'tracking',
                          icon: <LineChartOutlined />,
                          label: t('route.tracking'),
                          onClick: () => navigate(`/domain-knowledge/${item.id}/tracking`),
                        },
                        {
                          key: 'edit',
                          icon: <EditOutlined />,
                          label: t('common.edit'),
                          disabled: !item.can_write,
                          onClick: () => item.can_write && openEditModal(item),
                        },
                        { type: 'divider' },
                        {
                          key: 'delete',
                          icon: <DeleteOutlined />,
                          label: t('common.delete'),
                          danger: true,
                          disabled: !item.can_manage_permissions,
                          onClick: () => item.can_manage_permissions && setDeletingKb(item),
                        },
                      ],
                    }}
                    trigger={['hover']}
                    placement="bottomRight"
                  >
                    <EllipsisOutlined style={{ cursor: 'pointer' }} />
                  </Dropdown>
                </div>

                {/* Card Body - Description */}
                <div className="kb-card-body">
                  <div className="kb-card-desc">{item.description || t('domainKnowledge.noDescription')}</div>
                </div>

                {/* Card Tags Row */}
                <div className="kb-card-tags">
                  <div className="source-tags">
                    {item.grant_shared && (
                      <Tag color="purple" style={{ marginRight: 6 }}>
                        {t('kbPermission.grantShared')}
                      </Tag>
                    )}
                    {(item.dataSourceTypes && item.dataSourceTypes.length > 0 ? item.dataSourceTypes : ['file']).map(
                      (type) => (
                        <span key={type} className="source-tag">
                          {getSourceTypeDisplay(type)}
                        </span>
                      ),
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Custom Pagination */}
        {list.length > 0 && (
          <div className="yx-pagination">
            <span
              className={`yx-page-btn${page <= 1 ? ' disabled' : ''}`}
              onClick={() => page > 1 && setPage((p) => p - 1)}
            >
              &lt;
            </span>
            {pageNumbers().map((n) => (
              <span key={n} className={`yx-page-btn${n === page ? ' active' : ''}`} onClick={() => setPage(n)}>
                {n}
              </span>
            ))}
            <span
              className={`yx-page-btn${page >= totalPages ? ' disabled' : ''}`}
              onClick={() => page < totalPages && setPage((p) => p + 1)}
            >
              &gt;
            </span>
            <span className="yx-page-info">
              {t('domainKnowledge.pagination', {
                total,
                page,
                pages: totalPages,
              })}
            </span>
          </div>
        )}
      </div>

      <PermissionModal
        open={permOpen}
        currentKb={currentKb}
        members={permissionMembers}
        keyword={permissionKeyword}
        loading={permLoading}
        saving={permSaving}
        onKeywordChange={handlePermKeywordChange}
        onRoleChange={handlePermRoleChange}
        onSave={handleSavePermissions}
        onCancel={() => setPermOpen(false)}
      />

      <CreateEditModal
        open={createOpen}
        editingKb={editingKb}
        submitting={createSubmitting}
        onOk={handleCreate}
        onCancel={() => setCreateOpen(false)}
      />

      <DeleteConfirmModal
        open={!!deletingKb}
        deletingKb={deletingKb}
        submitting={deleteSubmitting}
        onConfirm={handleDelete}
        onCancel={() => setDeletingKb(null)}
      />
    </div>
  );
};

export default DomainKnowledge;
