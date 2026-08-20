import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';

function logoSrc(lng: string) {
  return lng === 'en' ? '/logo-en.svg' : '/logo.svg';
}
import { Form, Input, Button, Card, Select, message } from 'antd';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { login, setTokens, setUser, createLoginTicket } from '../../api/auth';
import type { TenantOption } from '../../api/auth';
import { isAllowedRedirect } from '@jonex/shell-sdk';
import { colors, radius } from '@jonex/platform-theme/tokens';
import { tenantDisplayName, userDisplayName } from '../../utils/userDisplay';

function getRedirectParam(): string | null {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('redirect');
  if (!raw) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return null;
  }
}

function getAppIdParam(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get('appId');
}

function createState(): string {
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, '0')).join('');
}

function getDevAllowedOrigins(): string[] {
  try {
    const raw = (import.meta as any).env?.VITE_ALLOWED_REDIRECT_ORIGINS || '';
    return raw
      ? raw
          .split(',')
          .map((s: string) => s.trim())
          .filter(Boolean)
      : [];
  } catch {
    return [];
  }
}

async function requestLoginTicket(appId: string, redirectUri: string, state: string): Promise<string | null> {
  try {
    const resp = await createLoginTicket(appId, redirectUri, state);
    return resp.ticket || null;
  } catch {
    console.warn('[Login] Failed to create login ticket, backend may not be ready');
    return null;
  }
}

interface LoginValues {
  username: string;
  password: string;
  tenantId?: string;
}

function LoginPage() {
  const { t, i18n } = useTranslation();
  const [form] = Form.useForm<LoginValues>();
  const [loading, setLoading] = useState(false);
  const [tenantOptions, setTenantOptions] = useState<TenantOption[]>([]);
  const navigate = useNavigate();

  const onFinish = async (values: LoginValues) => {
    setLoading(true);
    try {
      const result = await login(values.username, values.password, values.tenantId);
      if (result.status === 'tenant_selection_required') {
        setTenantOptions(result.tenant_options);
        form.setFieldValue('tenantId', undefined);
        message.info(t('shell.selectTenantToContinue'));
        return;
      }

      setTokens(result.access_token, result.refresh_token);
      setUser(result.user as unknown as Record<string, unknown>);
      message.success(t('shell.welcome', { name: userDisplayName(result.user, t) }));
      const redirectTo = getRedirectParam();
      if (redirectTo) {
        await handleRedirect(redirectTo);
      } else {
        navigate('/');
      }
    } catch (err: unknown) {
      const messageText =
        (err as { response?: { data?: { message?: string } } }).response?.data?.message ||
        (err as Error).message ||
        t('error.unknownError');
      message.error(`${t('auth.loginFailed')}: ${messageText}`);
    } finally {
      setLoading(false);
    }
  };

  function handleValuesChange(changedValues: Partial<LoginValues>) {
    if ('username' in changedValues || 'password' in changedValues) {
      setTenantOptions([]);
      form.setFieldValue('tenantId', undefined);
    }
  }

  async function handleRedirect(redirectTo: string) {
    const redirectUrl = new URL(redirectTo);

    // 真正同源：共享 localStorage，直接跳转
    if (redirectUrl.origin === window.location.origin) {
      window.location.href = redirectTo;
      return;
    }

    const isAllowed = isAllowedRedirect(redirectUrl, getDevAllowedOrigins());
    if (!isAllowed) {
      message.error(t('shell.redirectNotAllowed'));
      return;
    }

    const appId = getAppIdParam();
    if (!appId) {
      message.error(t('shell.missingRedirectAppId'));
      return;
    }

    const state = createState();
    const ticket = await requestLoginTicket(appId, redirectUrl.origin + redirectUrl.pathname, state);

    if (!ticket) {
      message.error(t('shell.loginSyncFailed'));
      return;
    }

    redirectUrl.searchParams.set('ticket', ticket);
    redirectUrl.searchParams.set('state', state);
    window.location.href = redirectUrl.toString();
  }

  return (
    <div
      style={{
        height: '100vh',
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        background: colors.bg,
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
      }}
    >
      <Card
        style={{
          width: 400,
          boxShadow: '0 4px 24px rgba(11, 43, 92, 0.08)',
          borderRadius: radius.card,
          border: `1px solid ${colors.borderLight}`,
          textAlign: 'center',
          paddingTop: 12,
        }}
        styles={{ body: { padding: '32px 40px' } }}
      >
        {/* Brand Logo */}
        <div style={{ marginBottom: 24, textAlign: 'center' }}>
          <img src={logoSrc(i18n.language)} alt={t('site.title')} style={{ height: 40, marginBottom: 16 }} />
          <p style={{ fontSize: 13, color: colors.textMuted, margin: 0 }}>{t('shell.loginSubtitle')}</p>
        </div>

        <Form
          name="login"
          form={form}
          onFinish={onFinish}
          onValuesChange={handleValuesChange}
          autoComplete="off"
          size="large"
        >
          <Form.Item name="username" rules={[{ required: true, message: t('shell.enterUsername') }]}>
            <Input
              prefix={<UserOutlined style={{ color: colors.textMuted }} />}
              placeholder={t('auth.username')}
              style={{ borderRadius: radius.btn, height: 44 }}
            />
          </Form.Item>

          <Form.Item name="password" rules={[{ required: true, message: t('shell.enterPassword') }]}>
            <Input.Password
              prefix={<LockOutlined style={{ color: colors.textMuted }} />}
              placeholder={t('auth.password')}
              style={{ borderRadius: radius.btn, height: 44 }}
            />
          </Form.Item>

          {tenantOptions.length > 0 && (
            <Form.Item name="tenantId" rules={[{ required: true, message: t('shell.selectTenant') }]}>
              <Select
                placeholder={t('shell.selectTenantPlaceholder')}
                style={{ textAlign: 'left' }}
                options={tenantOptions.map((tenant) => ({
                  value: tenant.tenant_id,
                  label: tenantDisplayName(tenant, t),
                }))}
              />
            </Form.Item>
          )}

          <Form.Item style={{ marginBottom: 12 }}>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              style={{
                height: 44,
                borderRadius: radius.btn,
                fontSize: 15,
                fontWeight: 500,
                background: colors.accent,
                borderColor: colors.accent,
              }}
            >
              {tenantOptions.length > 0 ? t('shell.continueLogin') : t('auth.login')}
            </Button>
          </Form.Item>
        </Form>

        {/* <div
          style={{
            padding: '10px 14px',
            background: colors.rowHover,
            borderRadius: 8,
            fontSize: 12,
            color: colors.textSecondary,
          }}
        >
          {t('shell.testAccount')}
        </div> */}
      </Card>
    </div>
  );
}

export default LoginPage;
