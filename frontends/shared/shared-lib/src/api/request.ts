import axios from 'axios';
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios';
import { emitSessionExpired, readAccessToken, refreshTokens } from '@jonex/shell-sdk';
import type { ApiError, ApiResponse } from './types';

export interface CreateRequestOptions {
  /** 请求根路径，默认 /api/v1 */
  baseURL?: string;
  timeout?: number;
  /** 自动注入 Authorization: Bearer <token> */
  injectToken?: boolean;
  /** 自动注入 X-Lang */
  injectLang?: boolean;
  /** 401 统一处理：清认证 + token 过期事件 + standalone 跳转（登录接口除外） */
  handle401?: boolean;
  /** 信封校验：code !== 0 视为业务失败并 reject */
  validateEnvelope?: boolean;
}

export interface ApiClient {
  /** 原始 axios 实例（未解包），需要原生响应时使用 */
  raw: AxiosInstance;
  /** GET，返回解包后的 data */
  get<T>(url: string, config?: AxiosRequestConfig): Promise<T>;
  post<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>;
  put<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>;
  patch<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>;
  delete<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>;
}

function toApiError(body: ApiResponse<unknown> | undefined, fallback: string): ApiError {
  const error = new Error(body?.message || fallback) as ApiError;
  error.bizCode = body?.code;
  error.requestId = body?.request_id;
  error.errorDetails = body?.error_details;
  return error;
}

function toHttpApiError(error: import('axios').AxiosError): ApiError {
  const body = error.response?.data as ApiResponse<unknown> | undefined;
  const status = error.response?.status;
  const apiError = new Error(
    body?.message || (status != null ? `Request failed (${status})` : '网络异常，请检查网络连接'),
  ) as ApiError;
  apiError.bizCode = body?.code;
  apiError.status = status;
  apiError.requestId = body?.request_id;
  apiError.errorDetails = body?.error_details;
  return apiError;
}

/**
 * 当前 UI 语言对应的后端 `X-Lang` 取值（与 `jonex_locale` 存储唯一同源）。
 *
 * axios 拦截器会自动注入；**裸 `fetch`（SSE 流式检索等）不走拦截器**，
 * 必须自己带上，否则后端按 `DEFAULT_LOCALE`（en-US）回文案，
 * 中文界面反而拿到英文错误。
 */
export function getLangHeader(): string {
  // node 环境（vitest 纯逻辑测试）没有 localStorage：按默认 en 处理，不抛错。
  const locale =
    typeof localStorage === 'undefined' ? 'en' : localStorage.getItem('jonex_locale') || 'en';
  return locale === 'en' ? 'en-US' : 'zh-CN';
}

/** 登录接口 401 语义为凭据错误，不做 token 过期处理（兼容相对路径 /auth/login 与绝对路径 /api/v1/auth/login） */
function isLoginRequest(config: AxiosRequestConfig | undefined): boolean {
  return /\/auth\/login(\?.*)?$/.test(config?.url ?? '');
}

/** 刷新接口自身 401 说明 refresh token 已失效，不能再次触发刷新（防递归） */
function isRefreshRequest(config: AxiosRequestConfig | undefined): boolean {
  return /\/auth\/refresh(\?.*)?$/.test(config?.url ?? '');
}

/** 已自动刷新重放过的请求：同一请求只重放一次，避免刷新后仍 401 时无限重试 */
const retriedRequests = new WeakSet<object>();

async function unwrap<T>(promise: Promise<AxiosResponse<ApiResponse<T>>>): Promise<T> {
  const response = await promise;
  return response.data.data;
}

/** 独立解包工具：兼容 `getData(request.get(url))` 的既有写法 */
export async function getData<T>(promise: Promise<AxiosResponse<ApiResponse<T>>>): Promise<T> {
  return unwrap(promise);
}

/**
 * 统一请求入口工厂：四端（token / X-Lang / 401 / 信封校验）一次性配齐。
 *
 * - 默认 baseURL=/api/v1，api 模块统一写相对路径；
 * - 拦截器只负责 reject 业务失败（code!==0）与 HTTP 错误，不提示错误；
 *   错误提示由页面 catch 统一处理（`message.error(err?.message || fallback)`）；
 * - `api.get<T>(url)` 直接返回解包后的 data，无需再手动 unwrap。
 */
export function createRequest(options: CreateRequestOptions = {}): ApiClient {
  const {
    baseURL = '/api/v1',
    timeout = 30000,
    injectToken = true,
    injectLang = true,
    handle401 = true,
    validateEnvelope = true,
  } = options;
  // 401 幂等处理：一次会话失效只清理/上报一次，避免页面并发 401 时重复 dispatch 事件与跳转
  let tokenExpiredHandled = false;

  const client = axios.create({ baseURL, timeout });

  client.interceptors.request.use((config) => {
    if (injectToken) {
      const token = readAccessToken();
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    if (injectLang) {
      config.headers['X-Lang'] = getLangHeader();
    }
    return config;
  });

  client.interceptors.response.use(
    (response) => {
      if (validateEnvelope) {
        const body = response.data as ApiResponse<unknown>;
        if (body && typeof body === 'object' && body.code !== undefined && body.code !== 0) {
          const error = toApiError(body, 'Request failed');
          return Promise.reject(error);
        }
      }
      return response;
    },
    async (error) => {
      if (axios.isAxiosError(error)) {
        const apiError = toHttpApiError(error);
        const config = error.config;
        const canAutoRetry =
          apiError.status === 401 &&
          handle401 &&
          !isLoginRequest(config) &&
          !isRefreshRequest(config) &&
          !tokenExpiredHandled &&
          (config == null || !retriedRequests.has(config));

        // 自动刷新并重放一次：access token 失效但 refresh token 仍有效时滑动续期，
        // 用户无感知；刷新失败或 refresh 也失效才真正走会话失效（emitSessionExpired）。
        if (canAutoRetry && config) {
          const ok = await refreshTokens();
          if (ok) {
            retriedRequests.add(config);
            // 用新 access token 重新发原请求
            config.headers.Authorization = `Bearer ${readAccessToken()}`;
            return client(config);
          }
        }

        if (apiError.status === 401 && handle401 && !isLoginRequest(config) && !tokenExpiredHandled) {
          tokenExpiredHandled = true;
          // 发会话失效信号（清 token + jonex:token-expired 事件），跳转由监听者统一执行；
          // 调试开关 jonex_disable_401_redirect 的判定已收敛在 shell-sdk emitSessionExpired 内。
          emitSessionExpired();
        }
        return Promise.reject(apiError);
      }
      return Promise.reject(error);
    },
  );

  return {
    raw: client,
    get: <T>(url: string, config?: AxiosRequestConfig) => unwrap<T>(client.get(url, config)),
    post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) => unwrap<T>(client.post(url, data, config)),
    put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) => unwrap<T>(client.put(url, data, config)),
    patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) => unwrap<T>(client.patch(url, data, config)),
    delete: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
      unwrap<T>(data !== undefined ? client.delete(url, { ...config, data }) : client.delete(url, config)),
  };
}
