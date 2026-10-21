import { afterEach, describe, expect, it, vi } from 'vitest';
import { SHELL_ROUTE_SYNC_EVENT, syncShellRoute, type ShellRouteSyncDetail } from './routeSync';

const ORIGIN = 'https://jonex.example';

interface Harness {
  events: { type: string; detail?: ShellRouteSyncDetail }[];
  /** 本窗地址栏 */
  selfHistory: string[];
  /** 顶层地址栏 —— iframe fallback 下 address bar 归父窗 */
  topHistory: string[];
  parentMessages: { data: unknown; targetOrigin: string }[];
}

function installGlobals({ embedded = false, crossOrigin = false } = {}): Harness {
  const selfHistory: string[] = [];
  const topHistory: string[] = [];
  const parentMessages: Harness['parentMessages'] = [];
  const events: Harness['events'] = [];

  class FakeCustomEvent<T = unknown> {
    detail?: T;
    constructor(
      public type: string,
      init?: { detail?: T },
    ) {
      this.detail = init?.detail;
    }
  }

  const fakeWindow: Record<string, unknown> = {
    location: { href: '', search: embedded ? '?embed=1' : '', origin: ORIGIN },
    history: {
      replaceState: (_state: unknown, _title: string, url: string) => {
        selfHistory.push(url);
      },
    },
    dispatchEvent: (event: FakeCustomEvent<ShellRouteSyncDetail>) => {
      events.push({ type: event.type, detail: event.detail });
      return true;
    },
  };

  // 父窗是独立对象（而不是本窗），才能分辨 replaceState 写到了谁的地址栏
  const parentWindow = {
    history: {
      replaceState: (_state: unknown, _title: string, url: string) => {
        topHistory.push(url);
      },
    },
    postMessage: (data: unknown, targetOrigin: string) => {
      parentMessages.push({ data, targetOrigin });
    },
  };

  if (crossOrigin) {
    Object.defineProperty(parentWindow, 'history', {
      get() {
        throw new Error('SecurityError: cross-origin frame');
      },
    });
    fakeWindow.parent = parentWindow;
  } else {
    fakeWindow.parent = embedded ? parentWindow : fakeWindow;
  }

  vi.stubGlobal('window', fakeWindow);
  vi.stubGlobal('CustomEvent', FakeCustomEvent);

  return { events, selfHistory, topHistory, parentMessages };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('syncShellRoute —— iframe fallback', () => {
  it('写父窗地址栏，并 postMessage 通知 shell', () => {
    const harness = installGlobals({ embedded: true });

    syncShellRoute('platform-management', '/task-center');

    expect(harness.topHistory).toEqual(['/apps/platform-management/task-center']);
    expect(harness.selfHistory).toEqual([]);
    expect(harness.parentMessages).toEqual([
      { data: { type: SHELL_ROUTE_SYNC_EVENT, path: '/apps/platform-management/task-center' }, targetOrigin: ORIGIN },
    ]);
  });

  it('带查询串与 hash 时原样拼在后面', () => {
    const harness = installGlobals({ embedded: true });

    syncShellRoute('core-business', '/documents/d-1/result?embed=1#top');

    expect(harness.topHistory).toEqual(['/apps/core-business/documents/d-1/result?embed=1#top']);
  });
});

describe('syncShellRoute —— hosted（同窗）', () => {
  it('写本窗地址栏，并派发同窗事件', () => {
    const harness = installGlobals();
    // hosted 由 RemoteApp 在 mount 时把 ShellContext 挂到本窗
    (window as unknown as { __SHELL_CONTEXT__?: unknown }).__SHELL_CONTEXT__ = {
      mode: 'hosted',
      basePath: '/apps/platform-management',
    };

    syncShellRoute('platform-management', '/task-center');

    expect(harness.selfHistory).toEqual(['/apps/platform-management/task-center']);
    expect(harness.events).toEqual([
      { type: SHELL_ROUTE_SYNC_EVENT, detail: { path: '/apps/platform-management/task-center' } },
    ]);
    expect(harness.parentMessages).toEqual([]);
  });

  it('有 shell 但没声明 basePath 时仍按 /apps/ 前缀', () => {
    const harness = installGlobals();
    (window as unknown as { __SHELL_CONTEXT__?: unknown }).__SHELL_CONTEXT__ = { mode: 'hosted' };

    syncShellRoute('core-business', '/domain-knowledge');

    expect(harness.selfHistory).toEqual(['/apps/core-business/domain-knowledge']);
  });
});

describe('syncShellRoute —— standalone 独立端口', () => {
  it('没有 shell 时用本应用前缀，不写 /apps/', () => {
    const harness = installGlobals();

    syncShellRoute('platform-management', '/users');

    expect(harness.selfHistory).toEqual(['/platform-management/users']);
  });
});

describe('syncShellRoute —— 边界', () => {
  it('根路由不同步，避免把地址栏擦成应用首页', () => {
    const harness = installGlobals({ embedded: true });

    syncShellRoute('platform-management', '/');
    syncShellRoute('platform-management', '/home');

    expect(harness.topHistory).toEqual([]);
    expect(harness.parentMessages).toEqual([]);
  });

  it('跨域父窗写不了地址栏时退回本窗，不抛异常', () => {
    const harness = installGlobals({ embedded: true, crossOrigin: true });

    expect(() => syncShellRoute('platform-management', '/task-center')).not.toThrow();

    expect(harness.selfHistory).toEqual(['/apps/platform-management/task-center']);
  });
});
