import axios from 'axios';
import type { ShellUser } from '@jonex/shell-sdk';
import i18n from '../locales/i18n';
import {
  JONEX_REFRESH_TOKEN_KEY,
  readAccessToken,
  writeAccessToken,
  readCachedUser,
  writeCachedUser,
  clearAuthStorage,
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

export const apiClient = axios.create({
  baseURL: '/',
  timeout: 30000,
});

apiClient.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const locale = localStorage.getItem('jonex_locale') || 'en';
  config.headers['X-Lang'] = locale === 'en' ? 'en-US' : 'zh-CN';
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (axios.isAxiosError(error)) {
      if (error.response?.status === 401) {
        import('antd').then(({ message }) => {
          message.error(i18n.t('auth.sessionExpired'));
        });
      }
      const raw = error.response?.data as Record<string, unknown> | undefined;
      const backendMsg = raw?.message || raw?.detail || raw?.error;
      if (typeof backendMsg === 'string') {
        return Promise.reject(new Error(backendMsg));
      }
    }
    return Promise.reject(error);
  },
);

interface ApiEnvelope<T> {
  success: boolean;
  code: number;
  message: string;
  data?: T;
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T>): T {
  if (!payload.success || !payload.data) {
    throw new Error(payload.message || 'Request failed');
  }
  return payload.data;
}

export interface TenantOption {
  tenant_id: string;
  tenant_name: string;
}

export interface AuthenticatedLoginResult {
  status: 'authenticated';
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in?: number;
  user: ShellUser;
}

export interface TenantSelectionLoginResult {
  status: 'tenant_selection_required';
  tenant_options: TenantOption[];
}

export type LoginFlowResult = AuthenticatedLoginResult | TenantSelectionLoginResult;

export interface LoginTicketResult {
  ticket: string;
  expires_in?: number;
}

export async function login(username: string, password: string, tenantId?: string): Promise<LoginFlowResult> {
  const resp = await apiClient.post<ApiEnvelope<LoginFlowResult>>(
    '/api/v1/auth/login',
    { username, password },
    tenantId ? { headers: { 'X-Tenant-ID': tenantId } } : undefined,
  );
  const result = unwrapEnvelope(resp.data);
  if (result.status === 'authenticated') {
    return { ...result, user: normalizeUser(result.user as unknown as Record<string, unknown>) };
  }
  return result;
}

export async function fetchCurrentUser(): Promise<ShellUser> {
  const resp = await apiClient.get<ApiEnvelope<Record<string, unknown>>>('/api/v1/auth/me');
  return normalizeUser(unwrapEnvelope(resp.data));
}

export async function createLoginTicket(appId: string, redirectUri: string, state: string): Promise<LoginTicketResult> {
  const resp = await apiClient.post<ApiEnvelope<LoginTicketResult>>('/api/v1/auth/login-ticket', {
    appId,
    redirectUri,
    state,
  });
  return unwrapEnvelope(resp.data);
}

export function logout(): void {
  clearTokens();
  window.location.href = '/login';
}

export default apiClient;
