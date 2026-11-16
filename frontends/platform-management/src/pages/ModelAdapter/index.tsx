import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Button, Tag, Spin, Result, message } from 'antd';
import {
  SearchOutlined,
  PlusOutlined,
  PlayCircleOutlined,
  SettingOutlined,
  RobotOutlined,
  CodepenOutlined,
  BlockOutlined,
  SortDescendingOutlined,
} from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { listProviders, testProvider, type ModelProviderItem } from '../../api/modelProviders';
import AdapterFormModal, { type AdapterFormModalHandle } from './AdapterFormModal';

const PROVIDER_TYPE_LABELS: Record<string, string> = {
  llm: 'LLM',
  embedding: 'Embedding',
  reranker: 'Reranker',
};

const BUILT_IN_MODEL_TYPE_KEYS: Record<string, string> = {
  provider_demo_gpt4o: 'modelAdapter.typeChat',
  provider_demo_claude: 'modelAdapter.typeChat',
  provider_demo_text2vec: 'modelAdapter.typeEmbedding',
  provider_demo_reranker: 'modelAdapter.typeReranking',
};

const ICON_CONFIG: { icon: React.ReactNode; color: string }[] = [
  { icon: <RobotOutlined />, color: '#10b981' },
  { icon: <CodepenOutlined />, color: '#8b5cf6' },
  { icon: <BlockOutlined />, color: 'var(--yx-accent)' },
  { icon: <SortDescendingOutlined />, color: '#f59e0b' },
];

function fmtLatency(ms: number | null): string {
  if (ms === null || ms === undefined) return '-';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function fmtCount(n: number | null): string {
  if (n === null || n === undefined) return '-';
  return n.toLocaleString();
}

export default function ModelAdapterPage() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const modalRef = useRef<AdapterFormModalHandle>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listProviders(0, 100);
      setProviders(result.items);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('modelAdapter.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = providers.filter((p) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return p.name.toLowerCase().includes(q) || (p.model_name || '').toLowerCase().includes(q);
  });

  const handleTest = async (id: string) => {
    try {
      const result = await testProvider(id);
      message.success(result.message || t('modelAdapter.testPassed'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('modelAdapter.testFailed'));
    }
  };

  if (loading) {
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
        title={t('modelAdapter.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <PageHeader title={t('modelAdapter.pageTitle')} />
      <PageContent>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 20,
            marginTop: 8,
          }}
        >
          <Input
            prefix={<SearchOutlined />}
            placeholder={t('modelAdapter.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
            style={{ width: 240 }}
          />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ fontSize: 13, color: 'var(--yx-text-secondary)' }}>
              {t('modelAdapter.totalModels', { count: filtered.length })}
            </span>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => modalRef.current?.open()}>
              {t('modelAdapter.addModel')}
            </Button>
          </div>
        </div>

        {filtered.length === 0 ? (
          <div className="yx-empty-state">
            <p>{t('modelAdapter.empty')}</p>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => modalRef.current?.open()}>
              {t('modelAdapter.addModel')}
            </Button>
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 20 }}>
            {filtered.map((p, idx) => {
              const iconCfg = ICON_CONFIG[idx % ICON_CONFIG.length];
              const modelTypeKey = BUILT_IN_MODEL_TYPE_KEYS[p.id];
              const modelTypeLabel = modelTypeKey ? t(modelTypeKey) : p.model_type || '-';
              return (
                <div
                  key={p.id}
                  style={{ background: '#fff', border: '1px solid var(--yx-border)', borderRadius: 12, padding: 24 }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
                    <div
                      style={{
                        width: 48,
                        height: 48,
                        borderRadius: 12,
                        background: iconCfg.color,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: 22,
                        color: '#fff',
                      }}
                    >
                      {iconCfg.icon}
                    </div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 600, fontSize: 16 }}>{p.name}</div>
                      <div style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>
                        {PROVIDER_TYPE_LABELS[p.provider_type] || p.provider_type} · {modelTypeLabel}
                      </div>
                    </div>
                    <Tag color={p.status === 'active' ? 'success' : 'default'}>
                      {p.status === 'active' ? t('modelAdapter.connected') : p.status}
                    </Tag>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 16 }}>
                    <div style={{ fontSize: 13 }}>
                      <span style={{ color: 'var(--yx-text-muted)' }}>{t('modelAdapter.latency')}: </span>
                      {fmtLatency(p.latency_ms)}
                    </div>
                    <div style={{ fontSize: 13 }}>
                      <span style={{ color: 'var(--yx-text-muted)' }}>
                        {p.token_limit
                          ? t('modelAdapter.tokenLimit') + ': '
                          : p.vector_dimension
                            ? t('modelAdapter.vectorDimension') + ': '
                            : t('modelAdapter.batchSizeTitle') + ': '}
                      </span>
                      {p.token_limit?.toLocaleString() ||
                        p.vector_dimension ||
                        ((p.config_json as Record<string, unknown>)?.batch_size as number) ||
                        '-'}
                    </div>
                    <div style={{ fontSize: 13 }}>
                      <span style={{ color: 'var(--yx-text-muted)' }}>{t('modelAdapter.callCount')}: </span>
                      {fmtCount(p.call_count)}
                    </div>
                    <div style={{ fontSize: 13 }}>
                      <span style={{ color: 'var(--yx-text-muted)' }}>{t('modelAdapter.successRate')}: </span>
                      {p.success_rate !== null ? `${p.success_rate}%` : '-'}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button type="primary" size="small" icon={<PlayCircleOutlined />} onClick={() => handleTest(p.id)}>
                      {t('modelAdapter.test')}
                    </Button>
                    <Button size="small" icon={<SettingOutlined />} onClick={() => modalRef.current?.openEdit(p)}>
                      {t('modelAdapter.configure')}
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <AdapterFormModal ref={modalRef} onSuccess={load} />
      </PageContent>
    </>
  );
}
