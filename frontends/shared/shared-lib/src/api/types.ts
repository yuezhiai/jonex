/** 后端统一响应信封 */
export interface ApiResponse<T = unknown> {
  request_id?: string;
  /** 部分能力服务返回 success 字段，统一保留兼容 */
  success?: boolean;
  code: number;
  message: string;
  data: T;
  error_details?: Record<string, unknown>;
  timestamp?: string;
}

/** 统一请求错误：携带业务码 / HTTP 状态 / 结构化详情 */
export interface ApiError extends Error {
  bizCode?: number;
  status?: number;
  requestId?: string;
  errorDetails?: Record<string, unknown>;
}
