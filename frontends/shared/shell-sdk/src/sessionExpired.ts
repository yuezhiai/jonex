import { clearAuthStorage } from './authStorage';
import { buildLoginRedirectUrl, stripBlockedAuthQuery, stripCallbackQuery } from './authRedirect';

/**
 * 统一登录跳转与会话失效信号。
 *
 * 全前端「跳转到登录页」的代码收敛到这里：
 * - redirectToLogin() 是全前端唯一执行 location.href 跳转的实现；
 * - emitSessionExpired() 是 401 / 会话失效的唯一信号出口（清 token + 发事件，不跳转）；
 * - installSessionExpiredRedirect() 让 shell 与 standalone 子应用挂载同一个跳转执行者。
 */

export interface RedirectToLoginOptions {
  /** 登录页地址，默认 '/login' */
  loginUrl: string;
  /** 子应用 id（跨域 ticket 交换用），shell 宿主可不传 */
  appId?: string;
  /** 会话已过期标记（?expired=1），仅 401/过期跳转使用；登出、首次引导不传 */
  expired?: boolean;
}

/** 当前页面 URL，清理敏感 query（jonex_token 等）与 ticket 回调参数 */
export function getCleanCurrentUrl(): string {
  const url = new URL(window.location.href);
  stripBlockedAuthQuery(url);
  stripCallbackQuery(url);
  return url.toString();
}

/** 唯一登录页跳转实现：所有跳转 login 的地方都必须走这里 */
export function redirectToLogin(options: RedirectToLoginOptions): void {
  let target = buildLoginRedirectUrl(options.loginUrl, getCleanCurrentUrl(), options.appId);
  if (options.expired) {
    const u = new URL(target, window.location.origin);
    u.searchParams.set('expired', '1');
    target = u.toString();
  }
  window.location.href = target;
}

/**
 * 会话失效信号：清 token + 发 jonex:token-expired 事件。
 *
 * 调试开关 localStorage['jonex_disable_401_redirect']='1' 时静默跳过
 * （不清 token 不发事件，仅由调用方 reject 给页面 catch），用于观察 401 不被踢回登录页。
 */
export function emitSessionExpired(): void {
  if (localStorage.getItem('jonex_disable_401_redirect') === '1') return;
  clearAuthStorage({ keepLocale: true });
  try {
    (window.top || window.parent || window).dispatchEvent(new CustomEvent('jonex:token-expired'));
  } catch {
    /* swallow */
  }
}

/** 挂载会话失效跳转（幂等）：监听 jonex:token-expired → redirectToLogin。返回取消函数 */
export function installSessionExpiredRedirect(options: Omit<RedirectToLoginOptions, 'expired'>): () => void {
  let handled = false;
  const handler = () => {
    if (handled) return;
    handled = true;
    redirectToLogin({ ...options, expired: true });
  };
  window.addEventListener('jonex:token-expired', handler);
  return () => window.removeEventListener('jonex:token-expired', handler);
}
