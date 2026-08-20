import axios from 'axios';
import { readAccessToken, clearAuthStorage } from '@jonex/shell-sdk';

export interface ApiResponse<T> {
  request_id?: string;
  success?: boolean;
  code: number;
  message: string;
  data: T;
  error_details?: Record<string, any>;
  timestamp?: string;
}

export interface ApiError extends Error {
  bizCode?: number;
  requestId?: string;
  errorDetails?: Record<string, any>;
}

function toApiError(body: ApiResponse<unknown> | undefined, fallback: string): ApiError {
  const e = new Error(body?.message || fallback) as ApiError;
  e.bizCode = body?.code;
  e.requestId = body?.request_id;
  e.errorDetails = body?.error_details;
  return e;
}

export const request = axios.create({
  baseURL: (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1',
  timeout: 30000,
});

request.interceptors.request.use((config) => {
  const token = readAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const locale = localStorage.getItem('jonex_locale') || 'en';
  config.headers['X-Lang'] = locale === 'en' ? 'en-US' : 'zh-CN';
  return config;
});

request.interceptors.response.use(
  (response) => {
    const body = response.data as ApiResponse<unknown>;
    if (body.code !== 0) {
      return Promise.reject(toApiError(body, 'Request failed'));
    }
    return response;
  },
  (error) => {
    if (axios.isAxiosError(error)) {
      if (error.response?.status === 401) {
        clearAuthStorage({ keepLocale: true });
        try {
          (window.top || window.parent || window).dispatchEvent(new CustomEvent('jonex:token-expired'));
        } catch {}
        // standalone 模式（无 Shell）自跳转
        if (window.parent === window && (window as any).__SHELL_CONTEXT__?.mode !== 'hosted') {
          window.location.href = `/login?redirect=${encodeURIComponent(window.location.href)}`;
        }
      }
      // 提取后端错误消息与结构化字段（bizCode / errorDetails），让调用方 err.message
      // 拿到真正的错误原因，同时能靠 bizCode / errorDetails 区分错误类型
      const body = error.response?.data as ApiResponse<unknown> | undefined;
      if (body?.message) {
        return Promise.reject(toApiError(body, 'Request failed'));
      }
    }
    return Promise.reject(error);
  },
);

export async function getData<T>(promise: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const response = await promise;
  return response.data.data;
}

export async function postData<T>(url: string, data?: unknown): Promise<T> {
  return getData<T>(request.post(url, data));
}

export async function putData<T>(url: string, data?: unknown): Promise<T> {
  return getData<T>(request.put(url, data));
}

export async function deleteData<T>(url: string, data?: unknown): Promise<T> {
  return getData<T>(data !== undefined ? request.delete(url, { data }) : request.delete(url));
}
