import { createRoot } from 'react-dom/client';
import App from './App';
import mount from './remote/RemoteApp';
import { bootstrapStandaloneAuth, createStandaloneShellContext, installSessionExpiredRedirect } from '@jonex/shell-sdk';
import './locales/i18n';
import './styles/index.scss';
import '@jonex/platform-theme/theme.css';
import '@jonex/platform-theme/layout.css';

const root = document.getElementById('root')!;
const shellContext = (window as any).__SHELL_CONTEXT__;

if (shellContext) {
  mount(root, shellContext);
} else {
  startStandalone();
}

async function startStandalone() {
  const appId = (import.meta as any).env?.VITE_APP_ID || 'ecosystem-management';
  const loginUrl = (import.meta as any).env?.VITE_LOGIN || '/login';
  const authMeUrl = (import.meta as any).env?.VITE_AUTH_ME || '/api/v1/auth/me';
  const exchangeTicketUrl = (import.meta as any).env?.VITE_AUTH_EXCHANGE_TICKET;
  const basePath = (import.meta as any).env?.VITE_STANDALONE_BASE || '/';

  // 统一会话失效跳转：401/过期事件 → 登录页（幂等，一次会话失效只跳一次）
  // 必须在 bootstrapStandaloneAuth 之前挂载：bootstrap 阶段 /auth/me 返回 401 会 emitSessionExpired() 发事件，
  // 监听器晚挂载会导致事件丢失、standalone 白屏不跳转
  installSessionExpiredRedirect({ loginUrl, appId });

  const result = await bootstrapStandaloneAuth({
    appId,
    loginUrl,
    authMeUrl,
    exchangeTicketUrl,
  });

  if (!result.authenticated) return;

  const ctx = createStandaloneShellContext({
    appId,
    basePath,
    token: result.token,
    user: result.user,
    loginUrl,
  });

  (window as any).__SHELL_CONTEXT__ = ctx;
  createRoot(root).render(<App />);
}
