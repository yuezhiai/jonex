import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Result, message } from 'antd';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import {
  listPromptTemplates,
  createPromptTemplate,
  updatePromptTemplate,
  copyPromptTemplate,
  type PromptTemplateItem,
  type VersionItem,
  type CreatePromptTemplatePayload,
  type UpdatePromptTemplatePayload,
} from '../../api/promptTemplates';
import { readPersistedSpaceId, onSpaceChanged } from '@jonex/shell-sdk';
import TemplateSidebar from './TemplateSidebar';
import TemplateReader from './TemplateReader';
import CreateEditModal from './CreateEditModal';
import VersionHistoryModal from './VersionHistoryModal';
import VersionDetailModal from './VersionDetailModal';
import DeleteConfirmModal from './DeleteConfirmModal';
import './index.css';

type ModalMode = 'create' | 'edit';
type Scope = 'system' | 'domain';

interface Snapshot {
  items: PromptTemplateItem[];
  total: number;
}

const EMPTY_SNAPSHOT: Snapshot = { items: [], total: 0 };

export default function PromptTemplates() {
  const { t } = useTranslation();

  // ── 列表数据 ──
  // system / domain 两个范围在同一筛选条件下一起拉取并各自缓存：
  // 切 tab 直接读缓存（零等待、无 loading 闪烁），筛选或领域空间变化时整体失效重拉。
  const [items, setItems] = useState<PromptTemplateItem[]>([]);
  const [listTotal, setListTotal] = useState(0);
  const [counts, setCounts] = useState({ system: 0, domain: 0 });
  // 仅在「没有任何内容可展示」时为真；后台刷新一律不动它，避免列表被 Spin 顶掉
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ── 筛选 ──
  const [scope, setScope] = useState<Scope>('system');
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState<string>('');
  // 当前领域空间 ID（模板按空间隔离，切换空间需重新拉取）
  const [domainSpaceId, setDomainSpaceId] = useState<string | null>(() => readPersistedSpaceId());

  useEffect(() => {
    return onSpaceChanged((spaceId) => {
      setDomainSpaceId(spaceId);
    });
  }, []);

  // ── 选中项（中栏）──
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // ── 弹窗 ──
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editingTemplate, setEditingTemplate] = useState<PromptTemplateItem | null>(null);
  const [versionModalOpen, setVersionModalOpen] = useState(false);
  const [versionTemplate, setVersionTemplate] = useState<PromptTemplateItem | null>(null);
  const [detailVersion, setDetailVersion] = useState<VersionItem | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // 筛选条件签名：变化即意味着缓存失效
  const filterKey = useMemo(
    () => `${keyword.trim()}::${category}::${domainSpaceId ?? ''}`,
    [keyword, category, domainSpaceId],
  );

  const scopeRef = useRef<Scope>(scope);
  // 每个范围上次选中的模板 id（切 tab 回来时恢复，避免中栏跳到第一条）
  const selectionRef = useRef<Record<Scope, string | null>>({ system: null, domain: null });
  const filterKeyRef = useRef(filterKey);
  // 缓存按「筛选条件 → 各范围快照」组织，只保留当前筛选条件下的数据
  const cacheRef = useRef<{ key: string; byScope: Record<Scope, Snapshot> } | null>(null);
  // 请求序号：丢弃过期响应，避免快速切换筛选时旧数据盖掉新数据
  const reqRef = useRef(0);

  useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  useEffect(() => {
    filterKeyRef.current = filterKey;
  }, [filterKey]);

  const applyScope = useCallback((target: Scope, byScope: Record<Scope, Snapshot>) => {
    const snapshot = byScope[target] ?? EMPTY_SNAPSHOT;
    setItems(snapshot.items);
    setListTotal(snapshot.total);
    setCounts({ system: byScope.system.total, domain: byScope.domain.total });
  }, []);

  /**
   * 拉取两个范围的列表。
   * silent=true 用于「后台静默校准」：不点亮 loading、不清空当前内容，失败也不打断页面。
   */
  const fetchAll = useCallback(
    async (options?: { silent?: boolean }) => {
      const key = filterKeyRef.current;
      const reqId = ++reqRef.current;
      if (!options?.silent) setLoading(true);
      setError(null);
      try {
        const base = {
          category: category || undefined,
          keyword: keyword.trim() || undefined,
        };
        const [systemResult, domainResult] = await Promise.all([
          listPromptTemplates({ ...base, scope: 'system', offset: 0, limit: 100 }),
          domainSpaceId
            ? listPromptTemplates({ ...base, domain_space_id: domainSpaceId, scope: 'domain', offset: 0, limit: 100 })
            : Promise.resolve({ items: [] as PromptTemplateItem[], total: 0, offset: 0, limit: 100 }),
        ]);
        // 过期响应（筛选条件已变 / 有更新的请求在飞）直接丢弃
        if (reqId !== reqRef.current || key !== filterKeyRef.current) return;
        const byScope: Record<Scope, Snapshot> = {
          system: { items: systemResult.items, total: systemResult.total },
          domain: { items: domainResult.items, total: domainResult.total },
        };
        cacheRef.current = { key, byScope };
        applyScope(scopeRef.current, byScope);
      } catch (err: unknown) {
        if (reqId === reqRef.current) {
          setError(err instanceof Error ? err.message : t('promptTemplate.loadError'));
        }
      } finally {
        if (reqId === reqRef.current) setLoading(false);
      }
    },
    [category, keyword, domainSpaceId, applyScope, t],
  );

  // 搜索防抖：输入 400ms 后才计入查询条件
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const nextKeyword = keywordInput.trim();
      setKeyword((prev) => (prev === nextKeyword ? prev : nextKeyword));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [keywordInput]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  // 局部改写缓存（新建/复制/更新/删除后直接改列表，不整页重拉）
  const patchCache = useCallback(
    (updater: (byScope: Record<Scope, Snapshot>) => Record<Scope, Snapshot>) => {
      const cache = cacheRef.current;
      if (!cache || cache.key !== filterKeyRef.current) return;
      cache.byScope = updater(cache.byScope);
      applyScope(scopeRef.current, cache.byScope);
    },
    [applyScope],
  );

  const upsertInList = useCallback(
    (item: PromptTemplateItem, target: Scope, position: 'top' | 'in-place') => {
      patchCache((byScope) => {
        const snapshot = byScope[target] ?? EMPTY_SNAPSHOT;
        const exists = snapshot.items.some((it) => it.id === item.id);
        if (!exists && position === 'in-place') return byScope; // 不在当前列表里就不硬插
        const nextItems = exists
          ? snapshot.items.map((it) => (it.id === item.id ? item : it))
          : [item, ...snapshot.items];
        return {
          ...byScope,
          [target]: { items: nextItems, total: snapshot.total + (exists ? 0 : 1) },
        };
      });
    },
    [patchCache],
  );

  const removeFromList = useCallback(
    (id: string) => {
      patchCache((byScope) => {
        const snapshot = byScope.domain ?? EMPTY_SNAPSHOT;
        const nextItems = snapshot.items.filter((it) => it.id !== id);
        if (nextItems.length === snapshot.items.length) return byScope;
        return { ...byScope, domain: { items: nextItems, total: Math.max(0, snapshot.total - 1) } };
      });
    },
    [patchCache],
  );

  // 选中项一律在渲染期推导（不放进 useEffect）：切 tab 的那一次提交里中栏就已经是新内容，
  // 否则会先渲染一帧「未选中」的空态再补上，看起来就是闪一下。
  // 每个范围各自记住上次选中的模板，切回来时不会跳到第一条。
  const rememberedId = selectionRef.current[scope];
  const selectedTemplate =
    items.find((item) => item.id === rememberedId) ??
    items.find((item) => item.id === selectedId) ??
    items[0] ??
    null;
  const activeSelectedId = selectedTemplate?.id ?? null;

  const handleSelect = (id: string) => {
    selectionRef.current[scope] = id;
    setSelectedId(id);
  };

  // ── 操作 ──
  const handleScopeChange = (next: Scope) => {
    if (next === scope) return;
    scopeRef.current = next;
    setScope(next);
    // 同一筛选条件下两个范围都已在缓存里：切 tab 直接换内容，不发请求、不闪 loading
    const cache = cacheRef.current;
    if (cache && cache.key === filterKeyRef.current) {
      applyScope(next, cache.byScope);
    } else {
      // 缓存缺失（首次未完成 / 上次拉取失败）时补一次静默拉取，别让列表停在上一个范围
      void fetchAll({ silent: true });
    }
  };

  const handleCreate = () => {
    setEditingTemplate(null);
    setModalMode('create');
    setModalOpen(true);
  };

  const handleEdit = (id: string) => {
    setEditingTemplate(items.find((item) => item.id === id) ?? null);
    setModalMode('edit');
    setModalOpen(true);
  };

  const handleVersionHistory = (id: string) => {
    setVersionTemplate(items.find((item) => item.id === id) ?? null);
    setVersionModalOpen(true);
  };

  const handleCopyTemplate = async (id: string) => {
    try {
      const created = await copyPromptTemplate(id, domainSpaceId ?? undefined);
      // 先把新模板插进当前列表（置顶，与服务端 updated_at 倒序一致），再静默校准
      if (created?.id) upsertInList(created, 'domain', 'top');
      message.success(t('promptTemplate.copyTemplateSuccess'));
      void fetchAll({ silent: true });
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('promptTemplate.copyFailed'));
    }
  };

  const handleSubmit = async (data: CreatePromptTemplatePayload | UpdatePromptTemplatePayload) => {
    if (modalMode === 'create') {
      const created = await createPromptTemplate(data as CreatePromptTemplatePayload, domainSpaceId ?? undefined);
      upsertInList(created, 'domain', 'top');
      selectionRef.current.domain = created.id;
      setSelectedId(created.id);
      message.success(t('promptTemplate.createSuccess'));
      void fetchAll({ silent: true });
    } else if (editingTemplate) {
      const updated = await updatePromptTemplate(
        editingTemplate.id,
        data as UpdatePromptTemplatePayload,
        domainSpaceId ?? undefined,
      );
      // 原地替换、不改行位置：避免保存后整个列表跳动
      upsertInList(updated, editingTemplate.scope, 'in-place');
      setSelectedId(updated.id);
      message.success(t('promptTemplate.updateSuccessWithVersion', { version: updated.current_version }));
    }
  };

  if (error && items.length === 0) {
    return (
      <Result
        status="error"
        title={t('promptTemplate.loadError')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void fetchAll()}>
            {t('promptTemplate.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <PageHeader title={t('promptTemplate.title')} />

      <PageContent showCard={false} style={{ overflow: 'hidden' }}>
        <div className="ptp-split">
          <TemplateSidebar
            scope={scope}
            onScopeChange={handleScopeChange}
            counts={counts}
            keywordInput={keywordInput}
            onKeywordChange={setKeywordInput}
            category={category}
            onCategoryChange={setCategory}
            total={listTotal}
            loading={loading}
            templates={items}
            selectedId={activeSelectedId}
            onSelect={handleSelect}
            onCreate={handleCreate}
            onRetry={() => void fetchAll()}
            hasDomainSpace={!!domainSpaceId}
          />
          <TemplateReader
            template={selectedTemplate}
            onEdit={handleEdit}
            onDelete={setDeletingId}
            onVersionHistory={handleVersionHistory}
            onCopyTemplate={handleCopyTemplate}
            canCopyToSpace={!!domainSpaceId}
          />
        </div>
      </PageContent>

      <CreateEditModal
        open={modalOpen}
        mode={modalMode}
        template={editingTemplate}
        domainSpaceId={domainSpaceId}
        onClose={() => setModalOpen(false)}
        onSubmit={handleSubmit}
      />

      <VersionHistoryModal
        open={versionModalOpen}
        template={versionTemplate}
        domainSpaceId={domainSpaceId}
        onClose={() => setVersionModalOpen(false)}
        onRollback={() => void fetchAll({ silent: true })}
        onViewDetail={(ver) => setDetailVersion(ver)}
      />

      <VersionDetailModal
        open={detailVersion !== null}
        version={detailVersion}
        onClose={() => setDetailVersion(null)}
      />

      <DeleteConfirmModal
        deletingId={deletingId}
        domainSpaceId={domainSpaceId}
        onClose={() => setDeletingId(null)}
        onDeleted={() => {
          if (deletingId) removeFromList(deletingId);
          void fetchAll({ silent: true });
        }}
      />
    </>
  );
}
