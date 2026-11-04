import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { Form, Input, Button, Card, Tabs, message } from 'antd';
import {
  ApartmentOutlined,
  LockOutlined,
  LoginOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import {
  login,
  setTokens,
  setUser,
  createLoginTicket,
  buildSsoAuthorizeUrl,
  fetchLoginConfig,
} from '../../api/auth';
import { isAllowedRedirect } from '@jonex/shell-sdk';
import { colors, radius } from '@jonex/platform-theme/tokens';
import { userDisplayName } from '../../utils/userDisplay';
import { markSsoNavigationInFlight, consumeSsoReturnHint } from '../../utils/ssoInflight';

function logoSrc(lng: string) {
  return lng === 'en' ? '/logo-en.svg' : '/logo.svg';
}

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

interface PasswordFormValues {
  username: string;
  password: string;
}

/**
 * Tab 内容区的固定高度（px），按「租户登录」Tab 的自然高度算：
 * 输入框 44 + `Form.Item` 下边距 24，提交按钮 44 + 下边距 12。
 *
 * 为什么要固定：两个 Tab 内容不等高，而卡片在满屏容器里垂直居中 —— 不固定的话，
 * 切到内容更少的「公司登录」时整张卡片会在视口里重排跳动。租户框被服务端配置隐藏时
 * 两个 Tab 各少一行，高度随之减掉 44 + 24。
 */
const PANE_MIN_HEIGHT = {
  withTenantField: 260,
  withoutTenantField: 192,
} as const;

function LoginPage() {
  const { t, i18n } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [ssoLoading, setSsoLoading] = useState(false);
  // 三态：null = 未知/加载中，false = 明确不显示，true = 显示。
  // 刻意不用 false 兼作未知：两者渲染上同为「不渲染」，但区分开来才让「还没拿到答案」
  // 与「答案就是隐藏」在类型与调试上分得清（请求失败落 false，见下方挂载 effect）。
  const [ssoEnabled, setSsoEnabled] = useState<boolean | null>(null);
  // 租户框是否隐藏：由服务端配置决定（GET /auth/login-config）。
  // 初值 false = 「配置未知时按显示处理」——配置还没回来（或拉取失败）就把框藏了、
  // 值又是空的，用户会无从下手；显示出来至少还能手输。
  const [hideTenantInput, setHideTenantInput] = useState(false);
  // 租户 ID 提升为页面 state，**不再是 Form 字段**：两个 Tab 要共用同一个值，而 antd Tabs
  // 会让「激活过的」面板保持挂载 —— 两份同名 Form.Item 并存时，校验错误会同时出现在
  // 两个（含隐藏的）面板里。提升成受控 state 后，切 Tab 天然保留已输入的值。
  const [tenantId, setTenantId] = useState('');
  // 租户为空时的提示。原先由 Form.Item 的 rules 承载，租户字段移出 Form 后自己管；
  // 仍然借 Form.Item 的 validateStatus / help 渲染，错误样式与其余字段保持一致。
  const [tenantError, setTenantError] = useState<string | null>(null);
  const navigate = useNavigate();

  // token 失效跳转（/login?expired=1）后在登录页展示提示，避免被页面跳转吞掉。
  // 提示后清理 URL 参数，防止登录页刷新时重复弹出。
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('expired') === '1') {
      message.error(t('auth.sessionExpired'));
      params.delete('expired');
      const url = new URL(window.location.href);
      url.search = params.toString();
      window.history.replaceState({}, '', url.toString());
    }
  }, [t]);

  // 企业 SSO 顶层导航失败后用户「后退」回本页 —— 给一次出口提示。
  // 文案刻意保持中性（见 i18n key 说明），不做任何归因：
  // 后端回调出错时是抛异常返回 JSON 而非重定向回 /login，故此处无从得知真实原因。
  useEffect(() => {
    if (consumeSsoReturnHint()) {
      message.warning(t('auth.ssoNotCompleted'));
    }
  }, [t]);

  // 支持以链接方式直达企业登录：/login?tenant=<tenant_id> 预填租户。
  // 企业用户通常不知道自己的租户 ID，由管理员/门户分发带参链接即可直达。
  // 租户 ID 非敏感值，保留在 URL 上以便刷新后仍能预填。
  useEffect(() => {
    const tenant = new URLSearchParams(window.location.search).get('tenant');
    if (tenant) setTenantId(tenant.trim());
  }, []);

  // 登录页启动配置（默认租户 + 是否隐藏租户框 + SSO 入口显隐），由服务端环境变量决定：
  // deploy/.env 的 DEFAULT_TENANT_ID / HIDE_TENANT_INPUT / HIDE_SSO_LOGIN 及部署级 SSO
  // 配置，经 GET /auth/login-config **一次请求**下发（SSO 入口字段一并下发）。
  // 前端不做业务判断（「配了隐藏却没有默认租户」由服务端解析成显示，见该接口说明）。
  //
  // SSO 入口显隐（cfg.enabled）也在同一次请求里下发，不再单独发第二个请求。
  //
  // 顺序：本 effect 与上面预填 effect 都在挂载时跑，但配置是异步拉回来的，
  // 所以 ?tenant= 一定先落值 —— 显式链接的意图优先于服务端默认值，这里只在框还空着时
  // 兜底填默认租户。改成函数式更新读最新值，因此不必把 tenantId 写进依赖数组 ——
  // 否则每敲一个字都会重发一次这个请求。
  //
  // 带 cancelled 守卫 —— 顶层导航（assign）后组件随即卸载，此时再 setState 会告警。
  useEffect(() => {
    let cancelled = false;
    void fetchLoginConfig()
      .then((cfg) => {
        if (cancelled) return;
        setHideTenantInput(cfg.hideTenantInput);
        setSsoEnabled(cfg.enabled);
        if (cfg.defaultTenantId) {
          setTenantId((current) => (current.trim() ? current : cfg.defaultTenantId));
        }
      })
      .catch(() => {
        // 仅**请求失败**回落为**隐藏**（网络错误 / 后端旧版本 404）：
        // 与本接口兜底值、HIDE_SSO_LOGIN 默认隐藏的语义一致 —— 拿不到配置时不冒出一个
        // 可能必然报错的按钮。
        if (!cancelled) setSsoEnabled(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 从 Keycloak 页面按浏览器「后退」返回本页时，浏览器可能用 bfcache 恢复
  // ssoLoading=true 的状态，导致按钮永久转圈；pageshow 时复位。
  //
  // 两处消费点（本 effect 与上面的挂载 effect）不是冗余，而是分工覆盖两条互斥的回跳路径，
  // 因为 dev 与 prod 的行为**相反**：
  //   - prod：nginx 给 SPA 文档下发 Cache-Control: no-store（deploy/nginx/shell.conf），
  //     Chromium 必定不走 bfcache → 后退是全量重载 → 挂载 effect 兜住；
  //   - dev：vite / dev-gateway 无该响应头 → 可能走 bfcache 恢复 → React 不重跑挂载 effect
  //     （已实测确认），只有本 pageshow 能兜住。
  // 少任何一处，就会有一端整条路径不提示。
  // （实测时序：pageshow 早于 React 被动 effect 触发，且监听器是在本 effect 内注册的，
  //   故首次加载时本处以「尚未注册」的方式缺席，真正消费的是挂载 effect —— 不会弹两次。）
  useEffect(() => {
    const reset = () => {
      setSsoLoading(false);
      if (consumeSsoReturnHint()) {
        message.warning(t('auth.ssoNotCompleted'));
      }
    };
    window.addEventListener('pageshow', reset);
    return () => window.removeEventListener('pageshow', reset);
  }, [t]);

  const onFinish = async (values: PasswordFormValues) => {
    const tenant = tenantId.trim();
    // 租户已不是 Form 字段，必填校验由这里兜住。hideTenantInput 时值来自服务端默认租户
    // （服务端保证「配了隐藏就一定有默认租户」），所以这一档只在手输模式下会触发。
    if (!tenant) {
      setTenantError(t('shell.enterTenantId'));
      return;
    }
    setLoading(true);
    try {
      const result = await login(values.username, values.password, tenant);
      setTokens(result.access_token, result.refresh_token, result.expires_in);
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

  // 公司登录 Tab：与密码登录共用同一份租户 ID（tenantId 决定 Keycloak realm）。
  // 顶层导航到后端 authorize 端点，由后端 302 到 Keycloak 登录页。
  // 失败（422/403/502）时后端返回 JSON，浏览器会直接渲染该 JSON —— 顶层导航
  // 无法被前端 catch，故这里只做点击前的纯客户端校验（租户非空）。
  const handleSsoLogin = () => {
    if (ssoLoading) return;
    const tenant = tenantId.trim();
    if (!tenant) {
      setTenantError(t('shell.enterTenantId'));
      return;
    }
    setSsoLoading(true);
    // 必须先于 assign：导航一旦发起，本行之后的代码不保证还会执行。
    // 哨兵跨文档存活，用户从 Keycloak 页面「后退」回来时由上面的 effect 消费。
    markSsoNavigationInFlight();
    window.location.assign(buildSsoAuthorizeUrl(tenant));
  };

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

  // 租户 ID 输入块：两个 Tab 共用同一个受控值，所以这里是一个 **JSX 变量**而不是组件 ——
  // 定义在函数体里的组件每次渲染都是新类型，React 会当成新组件卸载重建（输入框会失焦）。
  //
  // 租户框被配置隐藏时整块不渲染（原先靠 Form.Item 的 hidden 保留字段，现在值在 state 里，
  // 不渲染也不丢）。
  const tenantItem = (
    <Form.Item validateStatus={tenantError ? 'error' : undefined} help={tenantError ?? undefined}>
      <Input
        value={tenantId}
        onChange={(e) => {
          setTenantId(e.target.value);
          if (tenantError) setTenantError(null);
        }}
        prefix={<ApartmentOutlined style={{ color: colors.textMuted }} />}
        placeholder={t('shell.enterTenantId')}
        autoComplete="off"
        style={{ borderRadius: radius.btn, height: 44 }}
      />
    </Form.Item>
  );

  const tenantPane = (
    <Form name="login" onFinish={onFinish} autoComplete="off" size="large">
      {!hideTenantInput && tenantItem}

      <Form.Item name="username" rules={[{ required: true, message: t('shell.enterUsername') }]}>
        <Input
          prefix={<UserOutlined style={{ color: colors.textMuted }} />}
          placeholder={t('auth.username')}
          style={{ borderRadius: radius.btn, height: 44 }}
        />
      </Form.Item>

      <Form.Item
        name="password"
        rules={[
          { required: true, message: t('shell.enterPassword') },
          // 对齐后端校验：密码长度 8~128（auth_service.password_min_length 默认 8 / max_length 128）
          { min: 8, message: t('shell.passwordTooShort') },
          { max: 128, message: t('shell.passwordTooLong') },
        ]}
      >
        <Input.Password
          prefix={<LockOutlined style={{ color: colors.textMuted }} />}
          placeholder={t('auth.password')}
          style={{ borderRadius: radius.btn, height: 44 }}
        />
      </Form.Item>

      <Form.Item style={{ marginBottom: 12 }}>
        <Button
          type="primary"
          htmlType="submit"
          loading={loading}
          block
          icon={<LoginOutlined />}
          style={{
            height: 44,
            borderRadius: radius.btn,
            fontSize: 15,
            fontWeight: 500,
            background: colors.accent,
            borderColor: colors.accent,
          }}
        >
          {t('auth.login')}
        </Button>
      </Form.Item>
    </Form>
  );

  // 公司登录 Tab：只有租户 ID 与 SSO 按钮。按钮用 submit（而不是 onClick），
  // 这样在租户框里按回车即可发起 SSO，与租户登录 Tab 的体验对齐。
  //
  // 按钮取主色实心（与另一个 Tab 的「登录」同款）：它是本 Tab 唯一的主操作，
  // 用次要按钮样式会让两个 Tab 的视觉权重反过来。
  const companyPane = (
    <Form name="sso" onFinish={handleSsoLogin} autoComplete="off" size="large">
      {!hideTenantInput && tenantItem}

      <Form.Item style={{ marginBottom: 12 }}>
        <Button
          type="primary"
          htmlType="submit"
          block
          loading={ssoLoading}
          disabled={ssoLoading}
          icon={<SafetyCertificateOutlined />}
          style={{
            height: 44,
            borderRadius: radius.btn,
            fontSize: 15,
            fontWeight: 500,
            background: colors.accent,
            borderColor: colors.accent,
          }}
        >
          {ssoLoading ? t('shell.ssoRedirecting') : t('auth.signInSSO')}
        </Button>
      </Form.Item>

      <p style={{ fontSize: 12, color: colors.textMuted, margin: 0 }}>{t('shell.ssoRedirectHint')}</p>
    </Form>
  );

  // 只有「部署级 SSO 入口可见」才分 Tab。null（加载中/未知）与 false 都维持单表单 ——
  // 拿不到配置时不冒出一个可能必然报错的入口，页面与改动前逐像素一致。
  const showTabs = ssoEnabled === true;
  const paneMinHeight = hideTenantInput ? PANE_MIN_HEIGHT.withoutTenantField : PANE_MIN_HEIGHT.withTenantField;
  // 固定高度 + 内容顶对齐（flex-start）：两个 Tab 都以租户 ID 开头，顶对齐能让这个共用字段
  // 在切 Tab 时钉在原地、不上下跳 —— 垂直居中反而会让它随各 Tab 内容高度浮动。
  // 公司登录 Tab 下方留白是刻意的：内容少就短，不做拉伸。
  // flex 方向必须是 column，否则内容（Form）会缩成内容宽而不撑满卡片。
  const paneStyle = {
    minHeight: paneMinHeight,
    display: 'flex',
    flexDirection: 'column' as const,
  };

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

        {showTabs ? (
          <Tabs
            centered
            defaultActiveKey="tenant"
            tabBarStyle={{ marginBottom: 24 }}
            items={[
              {
                key: 'tenant',
                label: t('shell.loginTabTenant'),
                children: <div style={paneStyle}>{tenantPane}</div>,
              },
              {
                key: 'company',
                label: t('shell.loginTabCompany'),
                children: <div style={paneStyle}>{companyPane}</div>,
              },
            ]}
          />
        ) : (
          tenantPane
        )}

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
