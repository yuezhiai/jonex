import React, { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, Spin, Result, message } from 'antd';
import { PlusOutlined, SearchOutlined, GlobalOutlined, AppstoreOutlined } from '@ant-design/icons';
import { colors } from '@jonex/platform-theme/tokens';
import {
  listPromptTemplates,
  createPromptTemplate,
  updatePromptTemplate,
  copyPromptTemplate,
  PROMPT_CATEGORIES,
  PROMPT_CATEGORY_LABEL_KEYS,
  type PromptTemplateItem,
  type VersionItem,
  type CreatePromptTemplatePayload,
  type UpdatePromptTemplatePayload,
} from '../../api/promptTemplates';
import PromptCard from './PromptCard';
import CreateEditModal from './CreateEditModal';
import VersionModal from './VersionModal';
import { systemPromptTemplateDisplay } from '../../utils/systemPromptTemplateDisplay';
import { readPersistedSpaceId, onSpaceChanged } from '@jonex/shell-sdk';
import DeleteConfirmModal from './DeleteConfirmModal';
import VersionDetailModal from './VersionDetailModal';
import './index.css';

type ModalMode = 'create' | 'edit' | 'view';

export default function PromptTemplates() {
  const { t } = useTranslation();
  // Data state
  const [templates, setTemplates] = useState<PromptTemplateItem[]>([]);
  const [counts, setCounts] = useState({ system: 0, domain: 0 });
  const [listTotal, setListTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filter state
  const [scope, setScope] = useState<'system' | 'domain'>('system');
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState<string>('');
  // 当前领域空间 ID（用于空间隔离）
  const [domainSpaceId, setDomainSpaceId] = useState<string | null>(() => readPersistedSpaceId());

  // 订阅空间切换
  useEffect(() => {
    return onSpaceChanged((spaceId) => {
      setDomainSpaceId(spaceId);
    });
  }, []);

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editingTemplate, setEditingTemplate] = useState<PromptTemplateItem | null>(null);

  // Version modal
  const [versionModalOpen, setVersionModalOpen] = useState(false);
  const [versionTemplate, setVersionTemplate] = useState<PromptTemplateItem | null>(null);
  const [detailVersion, setDetailVersion] = useState<VersionItem | null>(null);

  // Delete state
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Load data
  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters = {
        category: category || undefined,
        keyword: keyword.trim() || undefined,
      };
      const spaceFilter = domainSpaceId ? { domain_space_id: domainSpaceId } : {};
      const [result, systemResult, domainResult] = await Promise.all([
        listPromptTemplates({
          ...filters,
          ...spaceFilter,
          scope,
          offset: 0,
          limit: 100,
        }),
        listPromptTemplates({
          ...filters,
          scope: 'system',
          offset: 0,
          limit: 1,
        }),
        listPromptTemplates({
          ...filters,
          ...spaceFilter,
          scope: 'domain',
          offset: 0,
          limit: 1,
        }),
      ]);
      setTemplates(result.items);
      setListTotal(result.total);
      setCounts({ system: systemResult.total, domain: domainResult.total });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('promptTemplate.loadError'));
    } finally {
      setLoading(false);
    }
  }, [scope, category, keyword, domainSpaceId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const nextKeyword = keywordInput.trim();
      setKeyword((prev) => (prev === nextKeyword ? prev : nextKeyword));
    }, 400);

    return () => window.clearTimeout(timer);
  }, [keywordInput]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Handlers
  const handleCreate = () => {
    setEditingTemplate(null);
    setModalMode('create');
    setModalOpen(true);
  };

  const handleEdit = (id: string) => {
    const tpl = templates.find((t) => t.id === id) || null;
    setEditingTemplate(tpl);
    setModalMode('edit');
    setModalOpen(true);
  };

  const handleView = (id: string) => {
    const tpl = templates.find((t) => t.id === id) || null;
    setEditingTemplate(tpl ? systemPromptTemplateDisplay(tpl, t) : null);
    setModalMode('view');
    setModalOpen(true);
  };

  const handleVersion = (id: string) => {
    const tpl = templates.find((t) => t.id === id) || null;
    setVersionTemplate(tpl);
    setVersionModalOpen(true);
  };

  const handleCopy = async (id: string) => {
    try {
      await copyPromptTemplate(id, domainSpaceId ?? undefined);
      message.success(t('promptTemplate.copyTemplateSuccess'));
      await loadData();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('promptTemplate.copyFailed'));
    }
  };

  const handleDeleteClick = (id: string) => {
    setDeletingId(id);
  };

  const handleSubmit = async (data: CreatePromptTemplatePayload | UpdatePromptTemplatePayload) => {
    if (modalMode === 'create') {
      await createPromptTemplate(data as CreatePromptTemplatePayload, domainSpaceId ?? undefined);
      message.success(t('promptTemplate.createSuccess'));
    } else if (editingTemplate) {
      const updated = await updatePromptTemplate(editingTemplate.id, data as UpdatePromptTemplatePayload, domainSpaceId ?? undefined);
      message.success(t('promptTemplate.updateSuccessWithVersion', { version: updated.current_version }));
    }
    await loadData();
  };

  // Counts
  const systemCount = counts.system;
  const domainCount = counts.domain;

  // ── Render ──

  if (loading && templates.length === 0) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error && templates.length === 0) {
    return (
      <Result
        status="error"
        title={t('promptTemplate.loadError')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={loadData}>
            {t('promptTemplate.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="pt-page">
      {/* Page header */}
      <div className="pt-header">
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: colors.brandDark, margin: 0 }}>
            📋 {t('promptTemplate.title')}
          </h1>
          <div style={{ fontSize: 14, color: '#94a3b8', marginTop: 4 }}>{t('promptTemplate.description')}</div>
        </div>
        {scope === 'domain' && (
          <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
            {t('promptTemplate.create')}
          </Button>
        )}
      </div>

      {/* Scope tabs */}
      <div className="pt-toolbar-card">
        <div className="pt-scope-tabs">
          <Button
            type={scope === 'system' ? 'primary' : 'default'}
            ghost={scope !== 'system'}
            icon={<GlobalOutlined />}
            onClick={() => setScope('system')}
            style={{ borderRadius: 8, display: 'inline-flex', alignItems: 'center', gap: 4 }}
          >
            {t('promptTemplate.systemScope')}
            <span
              style={{
                fontSize: 11,
                background: scope === 'system' ? 'rgba(255,255,255,0.3)' : '#e2e8f0',
                color: scope === 'system' ? '#fff' : '#64748b',
                padding: '1px 8px',
                borderRadius: 10,
                minWidth: 20,
                textAlign: 'center',
              }}
            >
              {systemCount}
            </span>
          </Button>
          <Button
            type={scope === 'domain' ? 'primary' : 'default'}
            ghost={scope !== 'domain'}
            icon={<AppstoreOutlined />}
            onClick={() => setScope('domain')}
            style={{ borderRadius: 8, display: 'inline-flex', alignItems: 'center', gap: 4 }}
          >
            {t('promptTemplate.domainScope')}
            <span
              style={{
                fontSize: 11,
                background: scope === 'domain' ? 'rgba(255,255,255,0.3)' : '#e2e8f0',
                color: scope === 'domain' ? '#fff' : '#64748b',
                padding: '1px 8px',
                borderRadius: 10,
                minWidth: 20,
                textAlign: 'center',
              }}
            >
              {domainCount}
            </span>
          </Button>
        </div>
      </div>

      {/* Search bar */}
      <div className="pt-toolbar-card">
        <div className="pt-search-bar">
          <Input
            prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
            placeholder={t('promptTemplate.searchPlaceholder')}
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            allowClear
            style={{ width: 320 }}
          />
          <Select
            value={category || 'all'}
            onChange={(v: string) => setCategory(v === 'all' ? '' : v)}
            style={{ width: 150 }}
          >
            <Select.Option value="all">{t('promptTemplate.allCategories')}</Select.Option>
            {PROMPT_CATEGORIES.map((cat) => (
              <Select.Option key={cat} value={cat}>
                {t(PROMPT_CATEGORY_LABEL_KEYS[cat] || cat)}
              </Select.Option>
            ))}
          </Select>
          <div style={{ flex: 1 }} />
          <span style={{ fontSize: 13, color: '#94a3b8' }}>{t('promptTemplate.countText', { count: listTotal })}</span>
        </div>
      </div>

      {/* Template grid */}
      {templates.length === 0 ? (
        <div className="pt-empty">
          <div style={{ fontSize: 40, marginBottom: 12, opacity: 0.4 }}>📂</div>
          <div style={{ color: '#94a3b8' }}>
            {scope === 'system' ? t('promptTemplate.noSystemTemplates') : t('promptTemplate.noDomainTemplates')}
          </div>
        </div>
      ) : (
        <div className="pt-grid">
          {templates.map((tpl) => (
            <PromptCard
              key={tpl.id}
              template={tpl}
              onEdit={handleEdit}
              onView={handleView}
              onDelete={handleDeleteClick}
              onVersion={handleVersion}
              onCopy={handleCopy}
            />
          ))}
        </div>
      )}

      {/* Create/Edit/View Modal */}
      <CreateEditModal
        open={modalOpen}
        mode={modalMode}
        template={editingTemplate}
        onClose={() => setModalOpen(false)}
        onSubmit={handleSubmit}
      />

      {/* Version Modal */}
      <VersionModal
        open={versionModalOpen}
        template={versionTemplate}
        onClose={() => setVersionModalOpen(false)}
        onRollback={loadData}
        onViewDetail={(ver) => setDetailVersion(ver)}
        domainSpaceId={domainSpaceId}
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
        onDeleted={loadData}
      />
    </div>
  );
}
