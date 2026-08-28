import { createRequest } from '@jonex/shared-lib';

// 拦截器不提示错误，错误提示由页面 catch 统一处理（message.error(err?.message || fallback)）
export default createRequest({ baseURL: '/api/v1' });
