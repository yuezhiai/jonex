// @vitest-environment jsdom
/**
 * 分级配置弹窗 · 用户密级 Tab 的组件级竞态测试（第 8 项）。
 *
 * 这里测的是**组件状态机**，不是纯函数：
 * - 用户密级加载用 `clearanceSeq` 代际守卫：先发后到的旧响应必须被丢弃；
 * - 无租户管理权限时连 Tab/请求都不出现。
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';

import ClassificationConfigModal from './ClassificationConfigModal';
import type { DgClassificationConfig } from '../types';
import {
  getClassificationDefault,
  listUserClearances,
} from '@/api/dataGovernance';

vi.mock('@/api/dataGovernance', () => ({
  getClassificationDefault: vi.fn(),
  listUserClearances: vi.fn(),
  setClassificationDefault: vi.fn(),
  setUserClearance: vi.fn(),
  upsertClassificationCategory: vi.fn(),
  upsertClassificationLevel: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const config: DgClassificationConfig = {
  levels: [
    { code: 'public', name: '公开', rank: 1, isActive: true },
    { code: 'internal', name: '内部', rank: 2, isActive: true },
    { code: 'confidential', name: '机密', rank: 3, isActive: true },
  ],
  categories: [],
};

const noop = () => {};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    },
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function modalElement(canManageTenant: boolean, open = true) {
  return (
    <ClassificationConfigModal
      open={open}
      config={config}
      loading={false}
      error={null}
      canManageTenant={canManageTenant}
      kbs={[]}
      onClose={noop}
      onChanged={noop}
      onRetry={noop}
    />
  );
}

function renderModal(canManageTenant: boolean, open = true) {
  return render(modalElement(canManageTenant, open));
}

describe('ClassificationConfigModal · 用户密级', () => {
  it('旧请求后到时不覆盖新请求的结果（代际守卫）', async () => {
    const first = deferred<Array<Record<string, unknown>>>();
    const second = deferred<Array<Record<string, unknown>>>();
    vi.mocked(getClassificationDefault).mockResolvedValue({
      scopeType: 'tenant',
      scopeId: '',
      levelCode: null,
      categories: null,
    });
    vi.mocked(listUserClearances)
      .mockReturnValueOnce(first.promise as never)
      .mockReturnValueOnce(second.promise as never);

    const view = renderModal(true);
    await waitFor(() => expect(listUserClearances).toHaveBeenCalledTimes(1));

    // 切到「用户密级」Tab
    fireEvent.click(
      await screen.findByRole('tab', {
        name: 'dataGovernance.classification.clearance.title',
      }),
    );

    // 快速关闭再打开：第二次加载发出，而第一次仍在途（真实竞态形状）
    view.rerender(modalElement(true, false));
    view.rerender(modalElement(true, true));
    await waitFor(() => expect(listUserClearances).toHaveBeenCalledTimes(2));
    // Modal 重新打开后 Tabs 回到首个 Tab，需要再切回用户密级
    fireEvent.click(
      await screen.findByRole('tab', {
        name: 'dataGovernance.classification.clearance.title',
      }),
    );

    // 新请求先返回
    second.resolve([
      {
        userId: '8',
        username: 'latest_user',
        displayName: '最新',
        maxLevelCode: 'internal',
        isActive: true,
      },
    ]);
    expect(await screen.findByText('latest_user')).toBeTruthy();

    // 旧请求后返回，必须被丢弃
    first.resolve([
      {
        userId: '7',
        username: 'stale_user',
        displayName: '过期',
        maxLevelCode: null,
        isActive: true,
      },
    ]);
    await waitFor(() => {
      expect(screen.queryByText('stale_user')).toBeNull();
    });
    expect(screen.getByText('latest_user')).toBeTruthy();
  });

  it('无租户管理权限时不显示用户密级 Tab，也不发请求', async () => {
    vi.mocked(getClassificationDefault).mockResolvedValue({
      scopeType: 'tenant',
      scopeId: '',
      levelCode: null,
      categories: null,
    });

    renderModal(false);

    await waitFor(() => {
      expect(listUserClearances).not.toHaveBeenCalled();
    });
    expect(
      screen.queryByText('dataGovernance.classification.clearance.title'),
    ).toBeNull();
  });
});
