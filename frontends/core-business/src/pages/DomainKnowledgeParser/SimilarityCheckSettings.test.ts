// @vitest-environment jsdom
import React from 'react';
import { message } from 'antd';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MockInstance } from 'vitest';

const { translate } = vi.hoisted(() => ({
  translate: (key: string) => key,
}));

vi.mock('react-i18next', () => ({
  // 真实 react-i18next 的 t 在语言不变时是稳定引用；测试桩也必须稳定，
  // 否则组件 useEffect 的 [t] 依赖每次渲染都会变化，导致反复重载。
  useTranslation: () => ({ t: translate }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getDomainKnowledgeDetail: vi.fn(),
  updateKnowledgeInfo: vi.fn(),
}));

import { getDomainKnowledgeDetail, updateKnowledgeInfo } from '@/api/domainKnowledge';
import en from '@/locales/en.json';
import zh from '@/locales/zh.json';
import SimilarityCheckSettings from './SimilarityCheckSettings';
import { effectiveThreshold, normalizeThresholdInput } from './SimilarityCheckSettings.utils';

const mockDetail = vi.mocked(getDomainKnowledgeDetail);
const mockUpdate = vi.mocked(updateKnowledgeInfo);

type Detail = Awaited<ReturnType<typeof getDomainKnowledgeDetail>>;

function baseDetail(overrides: Partial<Detail> = {}): Detail {
  return {
    similarityCheckEnabled: true,
    similarityCheckThreshold: 0.9,
    ...overrides,
  } as Detail;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSettings(canWrite = true, kbId = 'kb-1') {
  return render(React.createElement(SimilarityCheckSettings, { kbId, canWrite }));
}

async function waitForSwitch(): Promise<HTMLElement> {
  await waitFor(() => expect(screen.queryByRole('switch')).not.toBeNull());
  return screen.getByRole('switch');
}

async function waitForInput(): Promise<HTMLInputElement> {
  await waitFor(() => expect(screen.queryByRole('spinbutton')).not.toBeNull());
  return screen.getByRole('spinbutton') as HTMLInputElement;
}

type MessageMock = MockInstance<Parameters<typeof message.success>, ReturnType<typeof message.success>>;

let successSpy: MessageMock;
let warningSpy: MessageMock;
let errorSpy: MessageMock;

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

beforeEach(() => {
  mockDetail.mockReset();
  mockUpdate.mockReset();
  mockUpdate.mockResolvedValue({} as Awaited<ReturnType<typeof updateKnowledgeInfo>>);
  successSpy = vi.spyOn(message, 'success').mockReturnValue(undefined as never);
  warningSpy = vi.spyOn(message, 'warning').mockReturnValue(undefined as never);
  errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
});

afterEach(() => {
  cleanup();
  successSpy.mockRestore();
  warningSpy.mockRestore();
  errorSpy.mockRestore();
});

describe('similarity settings helpers', () => {
  it('KB 覆盖优先于全局默认', () => {
    expect(effectiveThreshold(0.9, 0.85)).toBe(0.9);
    expect(effectiveThreshold(null, 0.85)).toBe(0.85);
  });

  it('阈值输入限制在 0.5~0.999', () => {
    expect(normalizeThresholdInput('90')).toBe(0.9);
    expect(normalizeThresholdInput('30')).toBeNull();
    expect(normalizeThresholdInput('abc')).toBeNull();
  });

  it('zh/en 两份 locale 都带齐相似检测文案', () => {
    const keys = ['enable', 'hint', 'threshold', 'thresholdInvalid', 'reset', 'readonly'];
    for (const locale of [zh, en]) {
      const similarity = locale.parserConfig.similarity as Record<string, string>;
      keys.forEach((key) => expect(similarity[key]).toBeTruthy());
    }
  });
});

describe('SimilarityCheckSettings', () => {
  it('加载中返回 null，加载完成后显示开关', async () => {
    let resolveDetail!: (value: Detail) => void;
    mockDetail.mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      }) as ReturnType<typeof getDomainKnowledgeDetail>,
    );

    const { container } = renderSettings();
    expect(container.firstChild).toBeNull();

    resolveDetail(baseDetail());
    const toggle = await waitForSwitch();
    expect(toggle.getAttribute('aria-checked')).toBe('true');
  });

  it('阈值以一位小数展示（0.955 → 95.5）', async () => {
    mockDetail.mockResolvedValue(baseDetail({ similarityCheckThreshold: 0.955 }));
    renderSettings();

    const input = await waitForInput();
    expect(input.value).toBe('95.5');
  });

  it('只读时禁用开关并显示只读提示', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    renderSettings(false);

    const toggle = await waitForSwitch();
    expect(toggle.hasAttribute('disabled')).toBe(true);
    expect(screen.queryByRole('status')).not.toBeNull();
  });

  it('kbId 为空时不渲染', () => {
    mockDetail.mockResolvedValue(baseDetail());
    const { container } = render(React.createElement(SimilarityCheckSettings, { kbId: '', canWrite: true }));
    expect(container.firstChild).toBeNull();
    expect(mockDetail).not.toHaveBeenCalled();
  });

  it('失焦提交合法阈值时落库', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '95' } });
    fireEvent.blur(input);

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_threshold: 0.95 }));
  });

  it('Enter 提交合法阈值时落库', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '95' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', keyCode: 13 });

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_threshold: 0.95 }));
  });

  it('保存期间重复 Enter 不重复提交', async () => {
    const pending = deferred<Awaited<ReturnType<typeof updateKnowledgeInfo>>>();
    mockDetail.mockResolvedValue(baseDetail());
    mockUpdate.mockReturnValue(pending.promise);
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '95' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', keyCode: 13 });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', keyCode: 13 });

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    await act(async () => {
      pending.resolve({} as Awaited<ReturnType<typeof updateKnowledgeInfo>>);
    });
    expect(mockUpdate).toHaveBeenCalledTimes(1);
  });

  it('非法阈值不落库并提示', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '' } });
    fireEvent.blur(input);

    await waitFor(() => expect(warningSpy).toHaveBeenCalledWith('parserConfig.similarity.thresholdInvalid'));
    expect(mockUpdate).not.toHaveBeenCalled();
    expect(input.value).toBe('90.0');
  });

  it('阈值未变化（继承全局默认）不提交', async () => {
    mockDetail.mockResolvedValue(baseDetail({ similarityCheckThreshold: null }));
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '85' } });
    fireEvent.blur(input);

    await waitFor(() => expect(input.value).toBe('85.0'));
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it('阈值保存失败时回滚到上次有效值', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    mockUpdate.mockRejectedValueOnce(new Error('boom'));
    renderSettings();
    const input = await waitForInput();

    fireEvent.change(input, { target: { value: '95' } });
    fireEvent.blur(input);

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_threshold: 0.95 }));
    await waitFor(() => expect(input.value).toBe('90.0'));
  });

  it('开关保存失败时回滚', async () => {
    mockDetail.mockResolvedValue(baseDetail());
    mockUpdate.mockRejectedValueOnce(new Error('boom'));
    renderSettings();
    const toggle = await waitForSwitch();
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    fireEvent.click(toggle);

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_enabled: false }));
    await waitFor(() => expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true'));
  });

  it('重置阈值时提交 null', async () => {
    mockDetail.mockResolvedValue(baseDetail({ similarityCheckThreshold: 0.9 }));
    renderSettings();
    await waitForInput();

    fireEvent.click(screen.getByRole('button', { name: 'parserConfig.similarity.reset' }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_threshold: null }));
  });

  it('有脏值时点重置只发生重置', async () => {
    mockDetail.mockResolvedValue(baseDetail({ similarityCheckThreshold: 0.9 }));
    renderSettings();
    const input = await waitForInput();
    fireEvent.change(input, { target: { value: '95' } });

    const button = screen.getByRole('button', { name: 'parserConfig.similarity.reset' });
    const mouseDown = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    fireEvent(button, mouseDown);
    expect(mouseDown.defaultPrevented).toBe(true);
    fireEvent.click(button);

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    expect(mockUpdate.mock.calls[0][1]).toEqual({ similarity_check_threshold: null });
  });

  it('重置失败时回滚', async () => {
    mockDetail.mockResolvedValue(baseDetail({ similarityCheckThreshold: 0.9 }));
    mockUpdate.mockRejectedValueOnce(new Error('boom'));
    renderSettings();
    const input = await waitForInput();

    fireEvent.click(screen.getByRole('button', { name: 'parserConfig.similarity.reset' }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('kb-1', { similarity_check_threshold: null }));
    await waitFor(() => expect(input.value).toBe('90.0'));
  });

  it('加载失败只显示内联错误提示且不渲染开关', async () => {
    mockDetail.mockRejectedValue(new Error('load boom'));
    renderSettings();

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeNull());
    expect(screen.queryByRole('switch')).toBeNull();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});
