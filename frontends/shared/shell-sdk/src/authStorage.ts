export const JONEX_ACCESS_TOKEN_KEY = 'jonex_access_token';
export const JONEX_REFRESH_TOKEN_KEY = 'jonex_refresh_token';
export const JONEX_USER_KEY = 'jonex_user';
export const LEGACY_USER_INFO_KEY = 'userInfo';
export const JONEX_IMPERSONATION_BACKUP_KEY = 'jonex_impersonation_backup';

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
  storage.removeItem(JONEX_IMPERSONATION_BACKUP_KEY);
  LEGACY_AUTH_STORAGE_KEYS.forEach((key) => storage.removeItem(key));
  if (options.keepLocale && locale) storage.setItem('locale', locale);
}

export interface ImpersonationAuthBackup {
  accessToken: string | null;
  refreshToken: string | null;
}

export function backupAuthForImpersonation(): void {
  const storage = getStorage();
  if (!storage) return;
  const backup: ImpersonationAuthBackup = {
    accessToken: storage.getItem(JONEX_ACCESS_TOKEN_KEY),
    refreshToken: storage.getItem(JONEX_REFRESH_TOKEN_KEY),
  };
  storage.setItem(JONEX_IMPERSONATION_BACKUP_KEY, JSON.stringify(backup));
  // 模拟态仅保留 access token，清空 refresh（refresh 端点天然拒绝 type=user）
  storage.removeItem(JONEX_REFRESH_TOKEN_KEY);
}

export function restoreAuthFromImpersonation(): void {
  const storage = getStorage();
  if (!storage) return;
  const raw = storage.getItem(JONEX_IMPERSONATION_BACKUP_KEY);
  if (raw === null) return; // 幂等：无备份视为已恢复
  try {
    const backup = JSON.parse(raw) as ImpersonationAuthBackup;
    if (backup.accessToken) storage.setItem(JONEX_ACCESS_TOKEN_KEY, backup.accessToken);
    if (backup.refreshToken) storage.setItem(JONEX_REFRESH_TOKEN_KEY, backup.refreshToken);
  } catch {
    // 忽略损坏的备份
  }
  storage.removeItem(JONEX_IMPERSONATION_BACKUP_KEY);
}
