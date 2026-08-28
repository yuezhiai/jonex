import { createRequest } from '@jonex/shared-lib';
import type { ApiClient } from '@jonex/shared-lib';
import type { ShellUser } from '@jonex/shell-sdk';
import {
  JONEX_REFRESH_TOKEN_KEY,
  readAccessToken,
  writeAccessToken,
  readCachedUser,
  writeCachedUser,
  clearAuthStorage,
  backupAuthForImpersonation,
  restoreAuthFromImpersonation,
} from '@jonex/shell-sdk';

const LOCALE_KEY = 'locale';

export const getAccessToken = readAccessToken;
export const getUser = readCachedUser<ShellUser>;

function normalizeUser(raw: Record<string, unknown>): ShellUser {
  const id = String(raw.user_id || raw.id || '');
  const username = String(raw.username || '');
  const displayName = String(raw.display_name || raw.displayName || username);
  const tenantId = raw.tenant_id || raw.tenantId;
  const tenantName = raw.tenant_name || raw.tenantName;
  const roles: string[] = Array.isArray(raw.roles) ? (raw.roles as string[]) : raw.role ? [raw.role as string] : [];
  // 幂等：同时兜底 snake_case（后端原始）与 camelCase（已归一化对象二次进入）
  const isPlatformAdmin =
    raw.is_platform_admin === true || (raw as Record<string, unknown>).isPlatformAdmin === true;
  const isTenantAdmin =
    raw.is_tenant_admin === true || (raw as Record<string, unknown>).isTenantAdmin === true;
  const permissions: string[] = Array.isArray(raw.permissions) ? (raw.permissions as string[]) : [];
  const impersonated = raw.impersonated === true;
  const originalTenantId = raw.original_tenant_id || raw.originalTenantId;
  return {
    id,
    username,
    displayName,
    tenantId: tenantId ? String(tenantId) : undefined,
    tenantName: tenantName ? String(tenantName) : undefined,
    roles,
    isPlatformAdmin,
    isTenantAdmin,
    permissions,
    impersonated: impersonated || undefined,
    originalTenantId: originalTenantId ? String(originalTenantId) : undefined,
  };
}

export function setTokens(access: string, refresh: string): void {
  writeAccessToken(access);
  if (refresh) localStorage.setItem(JONEX_REFRESH_TOKEN_KEY, refresh);
}

export function setUser(user: Record<string, unknown>): void {
  const normalized = normalizeUser(user);
  writeCachedUser(normalized);
}

export function setLocale(locale: string): void {
  localStorage.setItem(LOCALE_KEY, locale);
}

export function getLocale(): string {
  return localStorage.getItem(LOCALE_KEY) || 'zh';
}

export function clearTokens(): void {
  clearAuthStorage();
}

/**
 * Clean up forbidden URL token params (jonex_token, jonex_user, jonex_refresh_token).
 * Only handles ticket/code/state for future ticket-exchange flow.
 */
export function initCrossOriginAuth(): void {
  const params = new URLSearchParams(window.location.search);
  const blockedKeys = ['jonex_token', 'jonex_user', 'jonex_refresh_token'];
  let changed = false;
  blockedKeys.forEach((key) => {
    if (params.has(key)) {
      params.delete(key);
      changed = true;
    }
  });
  if (changed) {
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState({}, '', url.toString());
  }
}

export function isAuthenticated(): boolean {
  return !!getAccessToken();
}

// 拦截器不提示错误，错误提示由页面 catch 统一处理（message.error(err?.message || fallback)）；
// 401 过期由 RequireAuth 的 jonex:token-expired 监听与轮询兜底统一跳转。
export const apiClient: ApiClient = createRequest({ baseURL: '/api/v1' });

export interface AuthenticatedLoginResult {
  status: 'authenticated';
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in?: number;
  user: ShellUser;
}

export interface LoginTicketResult {
  ticket: string;
  expires_in?: number;
}

export async function login(username: string, password: string, tenantId: string): Promise<AuthenticatedLoginResult> {
  const result = await apiClient.post<AuthenticatedLoginResult>(
    '/auth/login',
    { username, password },
    { headers: { 'X-Tenant-ID': tenantId } },
  );
  return { ...result, user: normalizeUser(result.user as unknown as Record<string, unknown>) };
}

export async function fetchCurrentUser(): Promise<ShellUser> {
  const raw = await apiClient.get<Record<string, unknown>>('/auth/me');
  return normalizeUser(raw);
}

export interface TenantListItem {
  id: string;
  name: string;
}

export interface ImpersonateResult {
  token: string;
  target_tenant_id: string;
  target_tenant_name?: string;
}

export async function listTenants(): Promise<{ items: TenantListItem[]; total: number }> {
  return apiClient.get<{ items: TenantListItem[]; total: number }>('/platform/tenants', {
    params: { page: 1, page_size: 100 },
  });
}

export async function impersonate(targetTenantId: string): Promise<ImpersonateResult> {
  return apiClient.post<ImpersonateResult>('/auth/impersonate', {
    target_tenant_id: targetTenantId,
  });
}

export async function endImpersonation(): Promise<void> {
  await apiClient.post<null>('/auth/impersonate/end');
}

/** 切换到目标租户：备份原 token → 写模拟 token → 刷新用户缓存 → 整页重载 */
export async function switchTenant(targetTenantId: string): Promise<void> {
  const result = await impersonate(targetTenantId);
  backupAuthForImpersonation();
  writeAccessToken(result.token);
  const freshUser = await fetchCurrentUser();
  writeCachedUser(freshUser);
  window.location.reload();
}

/** 退出模拟：记审计 → 还原原 token → 刷新用户缓存 → 整页重载 */
export async function exitImpersonation(): Promise<void> {
  await endImpersonation();
  restoreAuthFromImpersonation();
  const freshUser = await fetchCurrentUser();
  writeCachedUser(freshUser);
  window.location.reload();
}

export async function createLoginTicket(appId: string, redirectUri: string, state: string): Promise<LoginTicketResult> {
  return apiClient.post<LoginTicketResult>('/auth/login-ticket', {
    appId,
    redirectUri,
    state,
  });
}

export function logout(): void {
  clearTokens();
  window.location.href = '/login';
}

export default apiClient;
