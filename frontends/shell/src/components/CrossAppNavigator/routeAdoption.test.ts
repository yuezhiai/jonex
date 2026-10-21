import { describe, expect, it } from 'vitest';
import { shouldAdoptRoute } from './routeAdoption';

const KB_RESULT =
  '/apps/core-business/domain-knowledge/kb-1/documents/doc-1/result?embed=1';
const TASK_CENTER = '/apps/platform-management/task-center?embed=1';

describe('shouldAdoptRoute', () => {
  it('地址栏已经换到别的应用时采纳', () => {
    // 浏览器返回后 shell 的 router 还停在 core-business，地址栏实际已是 task-center
    expect(shouldAdoptRoute(KB_RESULT, TASK_CENTER)).toBe(true);
  });

  it('同一应用内部的跳转不采纳：采纳会重载 iframe、丢掉子应用的 SPA 状态', () => {
    expect(shouldAdoptRoute(TASK_CENTER, '/apps/platform-management/users')).toBe(false);
  });

  it('路径相同不采纳', () => {
    expect(shouldAdoptRoute(TASK_CENTER, TASK_CENTER)).toBe(false);
  });

  it('不归子应用协议的路径一律不采纳', () => {
    expect(shouldAdoptRoute('/login', '/login')).toBe(false);
    expect(shouldAdoptRoute(TASK_CENTER, '/apps/')).toBe(false);
    expect(shouldAdoptRoute(TASK_CENTER, '/apps')).toBe(false);
  });

  it('非字符串输入不采纳', () => {
    expect(shouldAdoptRoute(TASK_CENTER, undefined)).toBe(false);
    expect(shouldAdoptRoute(TASK_CENTER, { path: TASK_CENTER })).toBe(false);
  });
});
