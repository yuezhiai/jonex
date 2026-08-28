import React, { useState, useEffect, useCallback } from 'react';
import { Form, Input, Select, Button, message, Card, Spin, Result } from 'antd';
import {
  SaveOutlined,
  UndoOutlined,
  InfoCircleOutlined,
  SafetyOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { listSystemConfigs, updateSystemConfig } from '../../api/systemConfig';

interface ConfigMap {
  [key: string]: string;
}

const DEFAULTS: ConfigMap = {
  platform_name: 'Jonex',
  logo_url: '/assets/logo.png',
  default_language: 'zh',
  timezone: 'shanghai',
  session_timeout: '30',
  password_min_length: '8',
  login_lock_threshold: '5',
  lock_duration: '15',
};

export default function SystemConfig() {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await listSystemConfigs();
      const map: ConfigMap = {};
      r.items.forEach((c) => {
        map[c.config_key] = c.config_value || '';
      });
      const merged: ConfigMap = { ...DEFAULTS, ...map };
      form.setFieldsValue(merged);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, form]);

  useEffect(() => {
    load();
  }, [load]);

  const handleSave = async () => {
    if (changed.size === 0) {
      message.info(t('systemConfig.noChanges'));
      return;
    }
    setSaving(true);
    const values = form.getFieldsValue();
    let done = 0;
    let fail = 0;
    for (const key of changed) {
      const str = String(values[key] ?? '');
      try {
        await updateSystemConfig(key, str);
        done++;
      } catch {
        fail++;
      }
    }
    setSaving(false);
    if (fail === 0) {
      message.success(t('systemConfig.changesSaved', { count: done }));
      setChanged(new Set());
    } else {
      message.warning(t('systemConfig.changesSavedWithFail', { done, fail }));
    }
  };

  const handleReset = () => {
    form.setFieldsValue(DEFAULTS);
    setChanged(new Set(Object.keys(DEFAULTS)));
    message.info(t('systemConfig.resetToDefaultMsg'));
  };

  if (loading)
    return (
      <div style={{ display: 'flex', justifyContent: 'center', minHeight: 300, alignItems: 'center' }}>
        <Spin size="large" />
      </div>
    );
  if (error)
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  const changedStyle = { borderColor: '#f59e0b', boxShadow: '0 0 0 2px rgba(245,158,11,0.15)' };
  const fieldStyle = (key: string) => (changed.has(key) ? changedStyle : undefined);
  const grid = { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 } as const;
  const gridTop = { ...grid, marginTop: 14 } as const;

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('systemConfig.title')}</h1>
        <p style={{ color: '#64748b', margin: '4px 0 0', fontSize: 14 }}>{t('systemConfig.description')}</p>
      </div>

      <Form form={form} layout="vertical" onValuesChange={(cv) => setChanged((p) => new Set([...p, ...Object.keys(cv)]))}>
        <Card
          style={{ borderRadius: 12, border: '1px solid #e2e8f0', marginBottom: 20 }}
          styles={{ body: { padding: 24 } }}
        >
          <h3
            style={{
              margin: '0 0 16px',
              fontSize: 16,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              paddingBottom: 12,
              borderBottom: '1px solid #e2e8f0',
            }}
          >
            <InfoCircleOutlined style={{ color: '#3b82f6' }} /> {t('systemConfig.basicSettings')}
          </h3>
          <div style={grid}>
            <Form.Item name="platform_name" label={t('systemConfig.platformName')}>
              <Input style={fieldStyle('platform_name')} />
            </Form.Item>
            <Form.Item name="logo_url" label={t('systemConfig.logoUrl')}>
              <Input style={fieldStyle('logo_url')} />
            </Form.Item>
          </div>
          <div style={gridTop}>
            <Form.Item name="default_language" label={t('systemConfig.defaultLanguage')}>
              <Select
                style={{ width: '100%', ...(fieldStyle('default_language') || {}) }}
                options={[
                  { value: 'zh', label: t('systemConfig.chinese') },
                  { value: 'en', label: t('systemConfig.english') },
                ]}
              />
            </Form.Item>
            <Form.Item name="timezone" label={t('systemConfig.timezone')}>
              <Select
                style={{ width: '100%', ...(fieldStyle('timezone') || {}) }}
                options={[
                  { value: 'shanghai', label: 'Asia/Shanghai (UTC+8)' },
                  { value: 'utc', label: 'UTC' },
                ]}
              />
            </Form.Item>
          </div>
        </Card>

        <Card
          style={{ borderRadius: 12, border: '1px solid #e2e8f0', marginBottom: 20 }}
          styles={{ body: { padding: 24 } }}
        >
          <h3
            style={{
              margin: '0 0 16px',
              fontSize: 16,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              paddingBottom: 12,
              borderBottom: '1px solid #e2e8f0',
            }}
          >
            <SafetyOutlined style={{ color: '#3b82f6' }} /> {t('systemConfig.securitySettings')}
          </h3>
          <div style={grid}>
            <Form.Item name="session_timeout" label={t('systemConfig.sessionTimeout')}>
              <Input style={fieldStyle('session_timeout')} />
            </Form.Item>
            <Form.Item name="password_min_length" label={t('systemConfig.passwordMinLength')}>
              <Input style={fieldStyle('password_min_length')} />
            </Form.Item>
          </div>
          <div style={gridTop}>
            <Form.Item name="login_lock_threshold" label={t('systemConfig.loginLockThreshold')}>
              <Select
                style={{ width: '100%', ...(fieldStyle('login_lock_threshold') || {}) }}
                options={[
                  { value: '5', label: t('systemConfig.lockAfter5') },
                  { value: '3', label: t('systemConfig.lockAfter3') },
                  { value: '0', label: t('systemConfig.noLock') },
                ]}
              />
            </Form.Item>
            <Form.Item name="lock_duration" label={t('systemConfig.lockDuration')}>
              <Input style={fieldStyle('lock_duration')} />
            </Form.Item>
          </div>
        </Card>

        <div style={{ display: 'flex', gap: 12 }}>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
            {t('systemConfig.saveAll')}
          </Button>
          <Button icon={<UndoOutlined />} onClick={handleReset}>
            {t('systemConfig.resetToDefault')}
          </Button>
          {changed.size > 0 && (
            <span style={{ color: '#f59e0b', fontSize: 13, alignSelf: 'center' }}>
              {t('systemConfig.changedItems', { count: changed.size })}
            </span>
          )}
        </div>
      </Form>
    </div>
  );
}
