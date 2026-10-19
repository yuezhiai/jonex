import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, Empty, Form, InputNumber, Popconfirm, Result, Space, Spin, Tag, message } from 'antd';
import { ReloadOutlined, SafetyOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { usePermission } from '@jonex/shared-lib';
import {
  TENANT_SECURITY_WRITE_PERMISSION,
  getTenantSecurityConfig,
  resetTenantSecurityConfig,
  updateTenantSecurityConfig,
} from '../../api/tenantSecurity';
import type { TenantSecurityConfigItem, TenantSecurityConfigKey } from '../../api/tenantSecurity';
import {
  CARD_BODY_STYLE,
  CARD_STYLE,
  CENTERED_STYLE,
  CHANGED_FIELD_STYLE,
  GRID_STYLE,
  HEADING_STYLE,
  READONLY_HINT_STYLE,
  SUBTITLE_STYLE,
} from './styles';

/**
 * 租户安全设置 —— 系统配置页（`pages/SystemConfig`）里的一块，不再是独立页面。
 *
 * 为什么合并：它与「平台安全设置」改的是**同一组 4 个参数**，只是作用域不同
 * （平台全局默认值 vs 本租户覆盖值）。分成两个菜单入口时，用户得自己判断该去哪一页；
 * 同屏摆开后「平台默认值 → 本租户覆盖值」的回落关系一眼可见，也少一条菜单。
 *
 * 权限：路由守卫已保证进得来（`tenant:security:read`）；这里再按
 * `tenant:security:write` 决定**能否编辑** —— 只读角色仍可查看生效值、平台默认值与覆盖状态。
 */

/**
 * 4 个字段的**展示顺序**（与后端 tenant_security_config 的 key 对齐）。
 *
 * ⚠ 校验范围**不在这里**：由后端随 item 下发 `min`/`max`，前端直接用。
 * 前后端各写一份范围的后果是真实发生过的漂移 —— password_min_length 后端按 bcrypt
 * 截断定 72、前端若写 128，用户能填 128，点保存才吃 400。
 */
const FIELDS: Array<{ key: TenantSecurityConfigKey; labelKey: string }> = [
  { key: 'session_timeout', labelKey: 'tenantSecurity.sessionTimeout' },
  { key: 'password_min_length', labelKey: 'tenantSecurity.passwordMinLength' },
  { key: 'login_lock_threshold', labelKey: 'tenantSecurity.loginLockThreshold' },
  { key: 'lock_duration', labelKey: 'tenantSecurity.lockDuration' },
];

/** 仅当后端没下发 min/max 时兜底（老后端）；不设上限等于不做前端校验 */
const FALLBACK_RANGE = { min: 0, max: Number.MAX_SAFE_INTEGER };

type ConfigMap = Partial<Record<TenantSecurityConfigKey, TenantSecurityConfigItem>>;

/** config_value 后端统一按字符串下发，表单用数字；非法/空值回退 undefined 交给校验兜住 */
function toFormValue(raw: string | undefined): number | undefined {
  if (raw === undefined || raw === '') return undefined;
  const num = Number(raw);
  return Number.isFinite(num) ? num : undefined;
}

export default function TenantSecuritySection() {
  const { t } = useTranslation();
  const { hasPerm } = usePermission();
  const canWrite = hasPerm(TENANT_SECURITY_WRITE_PERMISSION);

  const [form] = Form.useForm();
  const [items, setItems] = useState<ConfigMap>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<Set<TenantSecurityConfigKey>>(new Set());
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState<TenantSecurityConfigKey | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await getTenantSecurityConfig();
      const map: ConfigMap = {};
      const values: Partial<Record<TenantSecurityConfigKey, number>> = {};
      (r.items || []).forEach((item) => {
        map[item.config_key] = item;
        values[item.config_key] = toFormValue(item.config_value);
      });
      setItems(map);
      form.setFieldsValue(values);
      setChanged(new Set());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [form, t]);

  useEffect(() => {
    load();
  }, [load]);

  const availableFields = FIELDS.filter((f) => items[f.key]);

  const handleSave = async () => {
    if (changed.size === 0) {
      message.info(t('tenantSecurity.noChanges'));
      return;
    }
    const keys = Array.from(changed);
    let values: Partial<Record<TenantSecurityConfigKey, number>>;
    try {
      // 只校验改动过的字段：未改动字段的旧值不阻断本次保存
      values = (await form.validateFields(keys)) as Partial<Record<TenantSecurityConfigKey, number>>;
    } catch {
      return; // 校验失败：antd 已在对应字段下渲染错误文案
    }

    setSaving(true);
    const okKeys: TenantSecurityConfigKey[] = [];
    let firstError: string | null = null;
    for (const key of keys) {
      try {
        const updated = await updateTenantSecurityConfig(key, String(values[key]));
        setItems((prev) => ({ ...prev, [key]: updated }));
        form.setFieldValue(key, toFormValue(updated.config_value));
        okKeys.push(key);
      } catch (e: unknown) {
        if (!firstError) firstError = e instanceof Error ? e.message : null;
      }
    }
    setSaving(false);

    const failedKeys = keys.filter((k) => !okKeys.includes(k));
    if (failedKeys.length === 0) {
      message.success(t('tenantSecurity.saveSuccess'));
      setChanged(new Set());
    } else {
      // 不吞错误：失败的字段保留「未保存」状态，提示后端返回的具体原因
      setChanged(new Set(failedKeys));
      message.error(firstError || t('tenantSecurity.saveFailed'));
    }
  };

  const handleReset = async (key: TenantSecurityConfigKey) => {
    setResetting(key);
    try {
      const updated = await resetTenantSecurityConfig(key);
      setItems((prev) => ({ ...prev, [key]: updated }));
      form.setFieldValue(key, toFormValue(updated.config_value));
      setChanged((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
      message.success(t('tenantSecurity.resetSuccess'));
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('tenantSecurity.resetFailed'));
    } finally {
      setResetting(null);
    }
  };

  const heading = (
    <h3 style={HEADING_STYLE}>
      <SafetyOutlined style={{ color: '#3b82f6' }} /> {t('tenantSecurity.title')}
      <Button
        icon={<ReloadOutlined />}
        size="small"
        onClick={load}
        loading={loading}
        disabled={loading}
        style={{ marginLeft: 'auto' }}
      >
        {t('common.refresh')}
      </Button>
    </h3>
  );

  // ① loading —— 只占本区块，不顶掉上面的平台部分
  if (loading) {
    return (
      <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
        {heading}
        <div style={CENTERED_STYLE}>
          <Spin size="large" />
        </div>
      </Card>
    );
  }

  // ② error（含重试）
  if (error) {
    return (
      <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
        {heading}
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
      </Card>
    );
  }

  // ③ empty（后端返回 0 项时不渲染空表单）
  if (availableFields.length === 0) {
    return (
      <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
        {heading}
        <Empty description={t('tenantSecurity.empty')}>
          <Button icon={<ReloadOutlined />} onClick={load}>
            {t('common.refresh')}
          </Button>
        </Empty>
      </Card>
    );
  }

  // ④ success（数据就绪，可编辑 + 保存 + 单项恢复默认）
  const fieldStyle = (key: TenantSecurityConfigKey) => (changed.has(key) ? CHANGED_FIELD_STYLE : undefined);

  return (
    <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
      {heading}
      <p style={SUBTITLE_STYLE}>{t('tenantSecurity.subtitle')}</p>
      {!canWrite && <p style={READONLY_HINT_STYLE}>{t('common.readOnlyHint')}</p>}
      <Form
        form={form}
        layout="vertical"
        onValuesChange={(cv) =>
          setChanged((prev) => new Set([...prev, ...(Object.keys(cv) as TenantSecurityConfigKey[])]))
        }
      >
        <div style={GRID_STYLE}>
          {availableFields.map((field) => {
            const item = items[field.key] as TenantSecurityConfigItem;
            // 范围以后端下发为准（缺失才兜底）——见 FIELDS 上方注释
            const min = item.min ?? FALLBACK_RANGE.min;
            const max = item.max ?? FALLBACK_RANGE.max;
            return (
              <Form.Item
                key={field.key}
                name={field.key}
                required
                label={
                  <Space size={4}>
                    <span>{t(field.labelKey)}</span>
                    <Tag color={item.is_overridden ? 'orange' : 'default'}>
                      {item.is_overridden ? t('tenantSecurity.overridden') : t('tenantSecurity.usingPlatformDefault')}
                    </Tag>
                  </Space>
                }
                rules={[
                  {
                    validator: (_rule, value) => {
                      if (value === undefined || value === null || value === '') {
                        return Promise.reject(new Error(t('tenantSecurity.requiredField')));
                      }
                      const num = Number(value);
                      if (!Number.isInteger(num) || num < min || num > max) {
                        return Promise.reject(new Error(t('tenantSecurity.rangeError', { min, max })));
                      }
                      return Promise.resolve();
                    },
                  },
                ]}
                extra={
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <span>{t('tenantSecurity.platformDefaultHint', { value: item.platform_value })}</span>
                    {item.is_overridden && canWrite ? (
                      <Popconfirm
                        title={t('tenantSecurity.resetConfirmTitle')}
                        description={t('tenantSecurity.resetConfirmDesc', { field: t(field.labelKey) })}
                        okText={t('common.confirm')}
                        cancelText={t('common.cancel')}
                        onConfirm={() => handleReset(field.key)}
                      >
                        <Button type="link" size="small" icon={<UndoOutlined />} loading={resetting === field.key}>
                          {t('tenantSecurity.resetToDefault')}
                        </Button>
                      </Popconfirm>
                    ) : null}
                  </div>
                }
              >
                <InputNumber
                  style={{ width: '100%', ...(fieldStyle(field.key) || {}) }}
                  precision={0}
                  min={min}
                  max={max}
                  disabled={!canWrite}
                />
              </Form.Item>
            );
          })}
        </div>

        {/* 操作行落在卡片内：这一侧的表单就是这一张卡片，保存范围与卡片范围一致 */}
        {canWrite && (
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
              {t('tenantSecurity.saveAll')}
            </Button>
            {changed.size > 0 && (
              <span style={{ color: '#f59e0b', fontSize: 13 }}>
                {t('tenantSecurity.changedItems', { count: changed.size })}
              </span>
            )}
          </div>
        )}
      </Form>
    </Card>
  );
}
