import React, { useState, useRef, useMemo, useEffect, useCallback } from 'react';
import { Button, Input, Select, Spin, Empty, Pagination, Tooltip } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined, SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TemplateDomain } from '../../api/templateDomains';
import { fetchDomains } from '../../api/templateDomains';
import { getTemplateDomainDisplay, isEnglishLanguage } from '../../utils/builtInTemplateDisplay';
import DomainFormModal, { type DomainFormModalHandle } from './DomainFormModal';
import DomainDeleteModal, { type DomainDeleteModalHandle } from './DomainDeleteModal';
import './index.css';

const PAGE_SIZE = 12;

const DOMAIN_ICONS: Array<{ icon: string; bg: string; color: string }> = [
  { icon: '💰', bg: '#eff6ff', color: '#3b82f6' },
  { icon: '🏥', bg: '#f0fdf4', color: '#10b981' },
  { icon: '⚖️', bg: '#fff7ed', color: '#f97316' },
  { icon: '🏭', bg: '#fef2f2', color: '#ef4444' },
  { icon: '📱', bg: '#f5f3ff', color: '#8b5cf6' },
  { icon: '🛒', bg: '#fdf2f8', color: '#ec4899' },
  { icon: '🎓', bg: '#ecfeff', color: '#06b6d4' },
  { icon: '🚚', bg: '#fffbeb', color: '#d97706' },
  { icon: '🏗️', bg: '#f1f5f9', color: '#475569' },
  { icon: '📊', bg: '#f0f9ff', color: '#0284c7' },
];

function getDomainIcon(index: number) {
  return DOMAIN_ICONS[index % DOMAIN_ICONS.length];
}

export default function TemplateDomains() {
  const navigate = useNavigate();
  const { t, i18n } = useTranslation();
  const english = isEnglishLanguage(i18n.resolvedLanguage || i18n.language);
  const [domains, setDomains] = useState<TemplateDomain[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const formModalRef = useRef<DomainFormModalHandle>(null);
  const deleteModalRef = useRef<DomainDeleteModalHandle>(null);

  const loadDomains = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const offset = (page - 1) * PAGE_SIZE;
      const result = await fetchDomains(offset, PAGE_SIZE);
      setDomains(result.items || []);
      setTotal(result.total || 0);
    } catch {
      setError(t('templateDomains.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  const filtered = useMemo(() => {
    return domains.filter((d) => {
      const display = getTemplateDomainDisplay(d, t, english);
      const query = search.trim().toLocaleLowerCase();
      if (query && !`${display.name} ${display.description} ${d.name}`.toLocaleLowerCase().includes(query))
        return false;
      if (statusFilter && d.status !== statusFilter) return false;
      return true;
    });
  }, [domains, search, statusFilter, t]);

  const openCreate = () => {
    formModalRef.current?.open();
  };

  const openEdit = (domain: TemplateDomain) => {
    formModalRef.current?.open(domain);
  };

  const openDeleteModal = (domain: TemplateDomain) => {
    deleteModalRef.current?.open(domain);
  };

  const handleCardClick = (domain: TemplateDomain) => {
    navigate(`/template-scenarios?domain_id=${domain.id}`);
  };

  const formatDate = (dateStr?: string | null) => {
    if (!dateStr) return '-';
    return dateStr.replace('T', ' ').substring(0, 16);
  };

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('templateDomains.pageTitle')}</h1>
        <p className="yx-page-subtitle">{t('templateDomains.pageSubtitle')}</p>
      </div>

      <div className="yx-toolbar">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <Input
            placeholder={t('templateDomains.searchPlaceholder')}
            prefix={<SearchOutlined />}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            allowClear
            style={{ width: 260 }}
          />
          <Select
            placeholder={t('status.allStatus')}
            value={statusFilter || undefined}
            onChange={(v) => {
              setStatusFilter(v || '');
              setPage(1);
            }}
            allowClear
            style={{ width: 130 }}
            options={[
              { label: t('status.active'), value: 'active' },
              { label: t('status.inactive'), value: 'inactive' },
            ]}
          />
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button icon={<ReloadOutlined />} onClick={loadDomains}>
            {t('common.refresh')}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t('templateDomains.createDomain')}
          </Button>
        </div>
      </div>

      {/* 加载 / 错误 / 空态 */}
      {loading && (
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin size="large" />
        </div>
      )}
      {error && !loading && (
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Empty description={error}>
            <Button type="primary" onClick={loadDomains}>
              {t('common.retry')}
            </Button>
          </Empty>
        </div>
      )}
      {!loading && !error && filtered.length === 0 && (
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Empty description={t('templateDomains.emptyText')}>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              {t('templateDomains.createFirst')}
            </Button>
          </Empty>
        </div>
      )}

      {/* 卡片网格 */}
      {!loading && !error && filtered.length > 0 && (
        <>
          <div className="domain-grid">
            {filtered.map((domain, index) => {
              const iconDef = getDomainIcon(index);
              const display = getTemplateDomainDisplay(domain, t, english);
              const statusLabelMap: Record<string, string> = {
                active: t('status.active'),
                inactive: t('status.inactive'),
                archived: t('status.archived'),
              };
              const statusClsMap: Record<string, string> = {
                active: 'active',
                inactive: 'inactive',
                archived: 'inactive',
              };
              return (
                <div key={domain.id} className="domain-card" onClick={() => handleCardClick(domain)}>
                  <div className="card-icon" style={{ background: iconDef.bg, color: iconDef.color }}>
                    {iconDef.icon}
                  </div>
                  <div className="card-body">
                    <Tooltip title={display.name}>
                      <div className="card-title">{display.name}</div>
                    </Tooltip>
                    {display.description ? (
                      <Tooltip title={display.description}>
                        <div className="card-desc">{display.description}</div>
                      </Tooltip>
                    ) : (
                      <div className="card-desc" style={{ color: '#cbd5e1' }}>
                        {t('templateDomains.noDescription')}
                      </div>
                    )}
                  </div>
                  <div className="card-meta">
                    <span className="card-time">{formatDate(domain.created_at)}</span>
                    <span className={`card-status ${statusClsMap[domain.status] || domain.status}`}>
                      {statusLabelMap[domain.status] || domain.status}
                    </span>
                  </div>
                  <div className="card-footer">
                    <span className="card-scenario-count">
                      {t('templateDomains.scenarioCount', { count: domain.scenario_count ?? 0 })}
                    </span>
                    <div className="card-actions">
                      <Button
                        type="text"
                        size="small"
                        icon={<EditOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          openEdit(domain);
                        }}
                      />
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          openDeleteModal(domain);
                        }}
                      />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 分页 */}
          {total > PAGE_SIZE && (
            <div style={{ textAlign: 'center', marginTop: 24 }}>
              <Pagination
                current={page}
                pageSize={PAGE_SIZE}
                total={total}
                onChange={(p) => setPage(p)}
                showSizeChanger={false}
              />
            </div>
          )}
        </>
      )}

      {/* 创建/编辑弹窗 */}
      <DomainFormModal ref={formModalRef} onSuccess={loadDomains} />

      {/* 删除确认弹窗 */}
      <DomainDeleteModal ref={deleteModalRef} onSuccess={loadDomains} />
    </div>
  );
}
