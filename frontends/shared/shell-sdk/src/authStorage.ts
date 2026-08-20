export const JONEX_ACCESS_TOKEN_KEY = 'jonex_access_token';
export const JONEX_REFRESH_TOKEN_KEY = 'jonex_refresh_token';
export const JONEX_USER_KEY = 'jonex_user';
export const LEGACY_USER_INFO_KEY = 'userInfo';

export const LEGACY_AUTH_STORAGE_KEYS = ['jonex_api_key', 'jonex_token'];

function getStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage;
}

export function readAccessToken(): string | null {
  return getStorage()?.getItem(JONEX_ACCESS_TOKEN_KEY) ?? null;
}

export function writeAccessToken(token: string): void {
  if (!token) return;
  getStorage()?.setItem(JONEX_ACCESS_TOKEN_KEY, token);
}

export function readRefreshToken(): string | null {
  return getStorage()?.getItem(JONEX_REFRESH_TOKEN_KEY) ?? null;
}

export function writeRefreshToken(token?: string | null): void {
  if (!token) return;
  getStorage()?.setItem(JONEX_REFRESH_TOKEN_KEY, token);
}

export function readCachedUser<T = unknown>(): T | null {
  const storage = getStorage();
  if (!storage) return null;
  let raw = storage.getItem(JONEX_USER_KEY);
  if (raw === null) {
    // 一次性迁移：历史遗留的 userInfo 旧键 → jonex_user（读后即迁并清除旧键）
    raw = storage.getItem(LEGACY_USER_INFO_KEY);
    if (raw !== null) {
      storage.setItem(JONEX_USER_KEY, raw);
      storage.removeItem(LEGACY_USER_INFO_KEY);
    }
  }
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function writeCachedUser(user: unknown): void {
  const storage = getStorage();
  if (!storage) return;
  // 统一只写 jonex_user；顺带清除历史残留的 userInfo 旧键
  storage.setItem(JONEX_USER_KEY, JSON.stringify(user));
  storage.removeItem(LEGACY_USER_INFO_KEY);
}

export function clearAuthStorage(options: { keepLocale?: boolean } = {}): void {
  const storage = getStorage();
  if (!storage) return;
  const locale = options.keepLocale ? storage.getItem('locale') : null;
  storage.removeItem(JONEX_ACCESS_TOKEN_KEY);
  storage.removeItem(JONEX_REFRESH_TOKEN_KEY);
  storage.removeItem(JONEX_USER_KEY);
  storage.removeItem(LEGACY_USER_INFO_KEY);
  LEGACY_AUTH_STORAGE_KEYS.forEach((key) => storage.removeItem(key));
  if (options.keepLocale && locale) storage.setItem('locale', locale);
}
