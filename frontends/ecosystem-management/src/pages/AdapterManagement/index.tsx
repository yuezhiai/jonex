import React, { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Tag, Spin, Result } from 'antd';
import {
  ThunderboltOutlined,
  ApiOutlined,
  CloudOutlined,
  SketchOutlined,
  CarryOutOutlined,
  BugOutlined,
} from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { listAdapters, type AdapterItem } from '../../api/adapters';
import './index.css';

const ADAPTER_DISPLAY: {
  type: string;
  icon: React.ReactNode;
  color: string;
}[] = [
  { type: 'dingtalk', icon: <ThunderboltOutlined />, color: 'var(--yx-accent)' },
  { type: 'wechat_work', icon: <ApiOutlined />, color: '#8b5cf6' },
  { type: 'feishu', icon: <CloudOutlined />, color: 'var(--yx-text-muted)' },
];

const FALLBACK_ICONS: { icon: React.ReactNode; color: string }[] = [
  { icon: <SketchOutlined />, color: 'var(--yx-text-muted)' },
  { icon: <CarryOutOutlined />, color: 'var(--yx-text-muted)' },
  { icon: <BugOutlined />, color: 'var(--yx-text-muted)' },
];

const BUILT_IN_ADAPTER_KEYS: Record<string, string> = {
  adapter_demo_dingtalk: 'adp',
  adapter_demo_wechat_agent: 'hiAgent',
  adapter_demo_feishu_analytics: 'quickSight',
  adapter_demo_dingtalk_ai: 'gemini',
  adapter_demo_wechat_workbench: 'workBuddy',
  adapter_demo_feishu_crawler: 'claw',
};

function getAdapterCopy(adapter: AdapterItem, t: (key: string) => string) {
  const key = BUILT_IN_ADAPTER_KEYS[adapter.id];
  return key
    ? {
        name: t(`ecosystem.builtInAdapters.${key}.name`),
        description: t(`ecosystem.builtInAdapters.${key}.description`),
      }
    : {
        name: adapter.name,
        description: (adapter.config_json as Record<string, string>)?.description || adapter.adapter_type,
      };
}

function getDisplay(idx: number) {
  return ADAPTER_DISPLAY[idx] || { icon: FALLBACK_ICONS[idx % 3].icon, color: FALLBACK_ICONS[idx % 3].color };
}

function getStatusBadge(status: string, t: (key: string) => string): { label: string; color: string } {
  switch (status) {
    case 'connected':
      return { label: t('ecosystem.adapterStatusConnected'), color: 'success' };
    case 'disconnected':
      return { label: t('ecosystem.adapterStatusPending'), color: 'warning' };
    case 'error':
      return { label: t('ecosystem.adapterStatusError'), color: 'error' };
    default:
      return { label: status, color: 'default' };
  }
}

export default function AdapterManagement() {
  const { t } = useTranslation();
  const [adapters, setAdapters] = useState<AdapterItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadAdapters = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listAdapters(0, 100);
      setAdapters(result.items);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    loadAdapters();
  }, [loadAdapters]);

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
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={loadAdapters}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <PageHeader title={t('ecosystem.adapterList')} />
      <PageContent showCard={false}>
        <div className="adapter-grid">
          {adapters.map((adapter, idx) => {
            const display = getDisplay(idx);
            const copy = getAdapterCopy(adapter, t);
            const badge = getStatusBadge(adapter.status, t);
            const isGrey = adapter.status !== 'connected';

            return (
              <div key={adapter.id} className={`adapter-card${isGrey ? ' grey' : ''}`}>
                {isGrey && <span className="future-tag">{t('ecosystem.comingSoon')}</span>}
                <div className="adapter-icon" style={{ background: isGrey ? 'var(--yx-text-muted)' : display.color }}>
                  {display.icon}
                </div>
                <h3>{copy.name}</h3>
                <div className="adapter-desc">{copy.description}</div>
                <div className="adapter-status">
                  <Tag color={badge.color} style={{ marginBottom: 0 }}>
                    {badge.label}
                  </Tag>
                </div>
              </div>
            );
          })}
        </div>
      </PageContent>
    </>
  );
}
