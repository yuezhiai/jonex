import React, { useState, useEffect, useCallback } from 'react';
import { Form, Input, InputNumber, Select, Button, message, Card, Spin, Result, Tabs } from 'antd';
import { UndoOutlined, InfoCircleOutlined, SafetyOutlined, SaveOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import {
  PLATFORM_CONFIG_READ_PERMISSION,
  PLATFORM_CONFIG_WRITE_PERMISSION,
  listSystemConfigs,
  updateSystemConfig,
} from '../../api/systemConfig';
import { TENANT_SECURITY_READ_PERMISSION } from '../../api/tenantSecurity';
import TenantSecuritySection from './TenantSecuritySection';
import {
  CARD_BODY_STYLE,
  CARD_STYLE,
  CENTERED_STYLE,
  CHANGED_FIELD_STYLE,
  GRID_STYLE,
  GRID_TOP_STYLE,
  HEADING_STYLE,
  READONLY_HINT_STYLE,
} from './styles';

/**
 * 系统配置页 —— 一个路由承载两套作用域，**按权限分成两个页签**：
 *
 * | 页签 | 权限码 | 作用对象 |
 * | --- | --- | --- |
 * | 平台设置（基本设置 + 平台安全设置） | `platform:config:read`（写：`platform:config:write`） | 平台全局共享的一份值 |
 * | 租户设置（租户安全设置） | `tenant:security:read`（写：`tenant:security:write`） | 本租户的覆盖值 |
 *
 * 租户安全设置原先是一个独立页面 + 独立菜单（`navigation.tenantSecurity`），现已并入本页，
 * 该菜单及其缓存行一并删除（见 `deploy/postgres/update/047`）。合并理由：两组参数**就是同一组**
 * （令牌有效期 / 密码强度 / 锁定策略），差别只在作用域；两页分开时用户得先猜自己该进哪一页。
 *
 * 只持一侧权限就**不渲染 tab 栏**（单页签的空壳没有信息量，还白多一层点击），
 * 所以租户管理员进来看到的就是租户设置本身，平台管理员看到两个页签。
 *
 * ⚠ 无平台码时**必须跳过** `listSystemConfigs()`：接口要 `platform:config:read`，
 *   租户管理员直接进本页会 403，把整个页面渲染成错误态，连带租户页签也看不到。
 */

interface ConfigMap {
  [key: string]: string;
}

interface TabItem {
  key: string;
  label: string;
  children: React.ReactNode;
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

/**
 * `login_lock_threshold` 的合法范围，**必须与后端 `SECURITY_CONFIG_DEFAULTS` 一致**
 * （`capabilities/platform/services/security_config_service.py`：min 0 / max 100，0 = 不锁定）。
 *
 * 这里原先是个三选一下拉（5 次 / 3 次 / 不锁定），把后端合法域砍成了 3 个值。而平台值正是
 * 租户继承的默认值、租户侧能填 0–100 的任意整数 —— 于是出现「租户能设 7、平台默认设不了 7」
 * 的倒挂，两侧控件也不一致（用户直接指出过）。改成数字输入后两侧同一个控件、同一个范围。
 * `tests/unit/test_tenant_security_config.py` 会拿后端那份元数据比对这里的字面量，改错即失败。
 */
const LOCK_THRESHOLD_MIN = 0;
const LOCK_THRESHOLD_MAX = 100;

export default function SystemConfig() {
  const { t } = useTranslation();
  const { hasPerm } = usePermission();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);

  // 平台全局配置（平台管理员）；租户管理员没有这个码，只有下面的租户区块
  const canPlatform = hasPerm(PLATFORM_CONFIG_READ_PERMISSION);
  const canPlatformWrite = hasPerm(PLATFORM_CONFIG_WRITE_PERMISSION);
  // 本租户覆盖值（租户管理员；平台管理员通常也持有，两块都看得到）
  const canTenant = hasPerm(TENANT_SECURITY_READ_PERMISSION);

  const load = useCallback(async () => {
    if (!canPlatform) {
      // 无平台码：不发请求（接口会 403），也不影响租户区块自行取数
      setLoading(false);
      return;
    }
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
  }, [t, form, canPlatform]);

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

  const fieldStyle = (key: string) => (changed.has(key) ? CHANGED_FIELD_STYLE : undefined);

  // 平台部分的三态（租户区块自己管自己的三态，互不遮蔽）
  const platformBlock = () => {
    if (loading)
      return (
        <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
          <div style={CENTERED_STYLE}>
            <Spin size="large" />
          </div>
        </Card>
      );
    if (error)
      return (
        <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
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

    return (
      <Form
        form={form}
        layout="vertical"
        onValuesChange={(cv) => setChanged((p) => new Set([...p, ...Object.keys(cv)]))}
      >
        <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
          <h3 style={HEADING_STYLE}>
            <InfoCircleOutlined style={{ color: '#3b82f6' }} /> {t('systemConfig.basicSettings')}
          </h3>
          <div style={GRID_STYLE}>
            <Form.Item name="platform_name" label={t('systemConfig.platformName')}>
              <Input style={fieldStyle('platform_name')} disabled={!canPlatformWrite} />
            </Form.Item>
            <Form.Item name="logo_url" label={t('systemConfig.logoUrl')}>
              <Input style={fieldStyle('logo_url')} disabled={!canPlatformWrite} />
            </Form.Item>
          </div>
          <div style={GRID_TOP_STYLE}>
            <Form.Item name="default_language" label={t('systemConfig.defaultLanguage')}>
              <Select
                style={{ width: '100%', ...(fieldStyle('default_language') || {}) }}
                disabled={!canPlatformWrite}
                options={[
                  { value: 'zh', label: t('systemConfig.chinese') },
                  { value: 'en', label: t('systemConfig.english') },
                ]}
              />
            </Form.Item>
            <Form.Item name="timezone" label={t('systemConfig.timezone')}>
              <Select
                style={{ width: '100%', ...(fieldStyle('timezone') || {}) }}
                disabled={!canPlatformWrite}
                options={[
                  { value: 'shanghai', label: 'Asia/Shanghai (UTC+8)' },
                  { value: 'utc', label: 'UTC' },
                ]}
              />
            </Form.Item>
          </div>
        </Card>

        <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
          <h3 style={HEADING_STYLE}>
            <SafetyOutlined style={{ color: '#3b82f6' }} /> {t('systemConfig.securitySettings')}
          </h3>
          {!canPlatformWrite && <p style={READONLY_HINT_STYLE}>{t('common.readOnlyHint')}</p>}
          <div style={GRID_STYLE}>
            <Form.Item name="session_timeout" label={t('systemConfig.sessionTimeout')}>
              <Input style={fieldStyle('session_timeout')} disabled={!canPlatformWrite} />
            </Form.Item>
            <Form.Item name="password_min_length" label={t('systemConfig.passwordMinLength')}>
              <Input style={fieldStyle('password_min_length')} disabled={!canPlatformWrite} />
            </Form.Item>
          </div>
          <div style={GRID_TOP_STYLE}>
            <Form.Item name="login_lock_threshold" label={t('systemConfig.loginLockThreshold')}>
              {/* 数字输入而非三档下拉：合法域是后端的 0–100（0 = 不锁定），与租户侧同一个控件 */}
              <InputNumber
                style={{ width: '100%', ...(fieldStyle('login_lock_threshold') || {}) }}
                min={LOCK_THRESHOLD_MIN}
                max={LOCK_THRESHOLD_MAX}
                precision={0}
                disabled={!canPlatformWrite}
              />
            </Form.Item>
            <Form.Item name="lock_duration" label={t('systemConfig.lockDuration')}>
              <Input style={fieldStyle('lock_duration')} disabled={!canPlatformWrite} />
            </Form.Item>
          </div>
        </Card>

        {/* 操作行**在两张卡片之外**：它保存的是这两张卡片里所有改动（同一个 form），
            放进任一卡片都会让人以为「只保存这张卡」。租户侧只有一张卡片，那边就放在卡内。 */}
        {canPlatformWrite && (
          <div style={{ display: 'flex', gap: 12, marginBottom: 20 }}>
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
        )}
      </Form>
    );
  };

  // 两个页签各自按权限出现：平台设置（platform:config:*）与租户设置（tenant:security:*）
  const tabItems: TabItem[] = [];
  if (canPlatform) {
    tabItems.push({ key: 'platform', label: t('systemConfig.tabs.platform'), children: platformBlock() });
  }
  if (canTenant) {
    tabItems.push({ key: 'tenant', label: t('systemConfig.tabs.tenant'), children: <TenantSecuritySection /> });
  }

  const renderBody = () => {
    // 正常到不了：菜单与路由守卫都要求至少持有一个码。兜底是为了不白屏。
    if (tabItems.length === 0) {
      return (
        <Card style={CARD_STYLE} styles={{ body: CARD_BODY_STYLE }}>
          <Result status="403" title={t('common.noPermission')} />
        </Card>
      );
    }
    // 只有一侧权限：不渲染单页签的空壳 tab 栏，直接给内容
    if (tabItems.length === 1) return tabItems[0].children;
    return <Tabs items={tabItems} />;
  };

  return (
    <>
      <PageHeader title={t('systemConfig.title')} />

      <PageContent showCard={false}>{renderBody()}</PageContent>
    </>
  );
}
