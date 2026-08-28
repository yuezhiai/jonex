import { createRequest, getData } from '@jonex/shared-lib';
import type { ApiClient, ApiError, ApiResponse } from '@jonex/shared-lib';

// 拦截器不提示错误，错误提示由页面 catch 统一处理（message.error(err?.message || fallback)）
const api: ApiClient = createRequest({
  baseURL: (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1',
});

/** 原始 axios 实例（兼容 `request.get` + `getData(request.get(...))` 既有写法） */
export const request = api.raw;

export { getData };

export async function postData<T>(
  url: string,
  data?: unknown,
  config?: import('axios').AxiosRequestConfig,
): Promise<T> {
  return api.post<T>(url, data, config);
}

export async function putData<T>(
  url: string,
  data?: unknown,
  config?: import('axios').AxiosRequestConfig,
): Promise<T> {
  return api.put<T>(url, data, config);
}

export async function deleteData<T>(
  url: string,
  data?: unknown,
  config?: import('axios').AxiosRequestConfig,
): Promise<T> {
  return api.delete<T>(url, data, config);
}

export type { ApiResponse, ApiError };
