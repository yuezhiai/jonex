import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Input, Button, message, Empty, Spin, Dropdown, Tag, Tooltip } from 'antd';
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
import { useQuota } from '@/hooks/useQuota';
import { SPACE_URL_PARAM } from '@jonex/shell-sdk';
import { PageHeader, PageContent } from '@jonex/shared-lib';
import type { DomainKnowledgeItem, KnowledgeBaseType } from '@/types/domainKnowledge';
import {
  getDomainKnowledgeList,
  createKnowledgeInfo,
  updateKnowledgeInfo,
  deleteKnowledgeInfo,
  getKbTypeOptions,
} from '@/api/domainKnowledge';
import { listAccessMethods } from '@/api/dataSource';
import { accessTypeDisplayName } from '@/utils/dataSourceDisplay';
import CreateEditModal from './CreateEditModal';
import DeleteConfirmModal from './DeleteConfirmModal';
import './index.scss';

// 一页取满后端 limit 上限（Query(le=100)），列表不再分页，底部分页器已移除
const PAGE_SIZE = 100;

// 描述被 CSS 截断为两行，仅溢出时 hover 出 tooltip：未溢出还弹会让用户以为内容被裁掉了
function OverflowTooltip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);

  return (
    <Tooltip
      title={text}
      open={open}
      // 关闭交给触发器（含移入弹层后的离开），我们只负责「溢出才允许打开」
      onOpenChange={(next) => {
        if (!next) setOpen(false);
      }}
      styles={{
        root: { maxWidth: 480, maxHeight: 240, overflowY: 'auto' },
        container: { whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
      }}
    >
      <div
        className="kb-card-desc"
        onMouseEnter={(e) => setOpen(e.currentTarget.scrollHeight > e.currentTarget.clientHeight + 1)}
      >
        {text}
      </div>
    </Tooltip>
  );
}

const DomainKnowledge = function DomainKnowledge() {
  const { global } = useStore();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // 租户级配额：知识库数量上限（达限禁用新建按钮；新建/删除后重新拉取刷新 used）
  const { getQuota, isReached, reload: reloadQuota } = useQuota();
  const kbQuota = getQuota('tenantKnowledgeBaseLimit');
  const kbReached = isReached('tenantKnowledgeBaseLimit');

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
  const [loading, setLoading] = useState(false);

  // ── create modal state ────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);

  // [jonex] 本部署可创建的知识库类型（后端 env `KB_ENABLED_TYPES`）。
  // null = 尚未拿到 —— 弹窗此时**不过滤**（fail-open）；真正的闸门在后端 create，
  // 所以拉取失败不该把用户挡在「一个类型都建不了」的死路上（见 CreateEditModal 注释）。
  const [kbTypeConfig, setKbTypeConfig] = useState<{
    kbTypes: KnowledgeBaseType[];
    default: KnowledgeBaseType;
  } | null>(null);

  const fetchKbTypes = useCallback(async () => {
    try {
      const config = await getKbTypeOptions();
      if (config.kbTypes.length) setKbTypeConfig(config);
    } catch {
      /* 保持 null = 不过滤；后端 create 仍会拒掉被禁类型并给出可读错误 */
    }
  }, []);

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
    async (kw: string) => {
      setLoading(true);
      try {
        const result = await getDomainKnowledgeList({
          page: 1,
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
    fetchKbTypes();
  }, []);

  // ── debounced keyword input → keyword ────────────────
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setKeyword(keywordInput);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [keywordInput]);

  // ── 统一的列表请求触发器 ────────────────────────────────
  // 由 spaces 就绪、当前空间、关键词共同驱动，任一变化都发起请求。
  useEffect(() => {
    if (global.spacesLoaded) {
      fetchList(keyword);
    }
  }, [global.spacesLoaded, global.currentSpaceId, keyword]);

  // ── create handler ───────────────────────────────────
  const handleCreate = async (values: { name: string; description?: string; kb_type?: KnowledgeBaseType }) => {
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
        // [jonex] 兜底顺序：表单值 → 后端给的默认可用类型 → 'lightrag'。
        // 直接写死 'lightrag' 会在它被 KB_ENABLED_TYPES 禁掉时提交一个必被拒的值。
        await createKnowledgeInfo({
          ...data,
          kb_type: values.kb_type ?? kbTypeConfig?.default ?? 'lightrag',
        });
        message.success(t('domainKnowledge.knowledgeBaseCreated'));
      }
      setCreateOpen(false);
      setEditingKb(null);
      fetchList(keyword);
      if (!editingKb) reloadQuota(); // 新建会改变知识库数量配额 used，编辑不改变
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
      fetchList(keyword);
      reloadQuota(); // 删除会释放知识库数量配额 used
    } catch (err: any) {
      message.error(err?.message || t('common.deleteFailed'));
    } finally {
      setDeleteSubmitting(false);
    }
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
    <>
      {/* Page Header：标题与当前空间筛选信息同行 */}
      <PageHeader
        title={t('navigation.domainKnowledge')}
        extra={
          <div className="yx-filter-row">
            <label>
              <GlobalOutlined style={{ color: '#3b82f6' }} /> {t('domainKnowledge.currentSpace')}
            </label>
            <span style={{ fontWeight: 500, color: '#0b2b5c' }}>
              {global.currentSpace?.name || t('domainKnowledge.notSelected')}
            </span>
            <span className="yx-filter-count">{t('domainKnowledge.knowledgeBaseCount', { total })}</span>
          </div>
        }
      />

      {/* Toolbar + Card Grid */}
      <PageContent showCard={false}>
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
          {!global.currentSpaceId ? (
            <span style={{ color: '#f97316', fontSize: 13 }}>{t('domainKnowledge.notSelected')}</span>
          ) : kbReached ? (
            <span style={{ color: '#f97316', fontSize: 13 }}>
              {t('common.quotaKbReached', { limit: kbQuota?.limit ?? 0 })}
            </span>
          ) : (
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!global.currentSpace?.can_write_space}
              onClick={openCreateModal}
            >
              {t('domainKnowledge.newKnowledgeBase')}
            </Button>
          )}
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
              {!global.currentSpaceId ? (
                <div style={{ color: '#f97316', fontSize: 13, marginBottom: 12 }}>
                  {t('domainKnowledge.notSelected')}
                </div>
              ) : kbReached ? (
                <div style={{ color: '#f97316', fontSize: 13, marginBottom: 12 }}>
                  {t('common.quotaKbReached', { limit: kbQuota?.limit ?? 0 })}
                </div>
              ) : (
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  disabled={!global.currentSpace?.can_write_space}
                  onClick={openCreateModal}
                >
                  {t('domainKnowledge.newKnowledgeBase')}
                </Button>
              )}
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
                      {/* [jonex] 知识库类型标签：三种类型行为差别大（引用溯源/编译结果页），需可一眼区分 */}
                      <Tag
                        color={item.kbType === 'jonex_wiki' ? 'magenta' : item.kbType === 'openkb' ? 'purple' : 'blue'}
                        style={{ marginInlineEnd: 0, flexShrink: 0, fontSize: 12 }}
                      >
                        {item.kbType === 'jonex_wiki'
                          ? t('domainKnowledge.kbTypeJonexWiki')
                          : item.kbType === 'openkb'
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
                          // 带上来源：设置页据此把「返回」指回本列表
                          onClick: () =>
                            navigate(`/domain-knowledge/${item.id}/detail`, {
                              state: { backTo: '/domain-knowledge' },
                            }),
                        },
                        {
                          key: 'tracking',
                          icon: <LineChartOutlined />,
                          label: t('route.tracking'),
                          // 追踪已收进知识库详情页的 tab，原地跳转并直接落在那一个 tab 上
                          onClick: () => navigate(`/domain-knowledge/${item.id}?tab=tracking`),
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
                  <OverflowTooltip text={item.description || t('domainKnowledge.noDescription')} />
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
      </PageContent>

      <CreateEditModal
        open={createOpen}
        editingKb={editingKb}
        submitting={createSubmitting}
        onOk={handleCreate}
        onCancel={() => setCreateOpen(false)}
        kbTypes={kbTypeConfig?.kbTypes}
        defaultKbType={kbTypeConfig?.default}
      />

      <DeleteConfirmModal
        open={!!deletingKb}
        deletingKb={deletingKb}
        submitting={deleteSubmitting}
        onConfirm={handleDelete}
        onCancel={() => setDeletingKb(null)}
      />
    </>
  );
};

export default DomainKnowledge;
