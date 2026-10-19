// @vitest-environment jsdom
/**
 * [bbox-port] DocumentViewer 决策测试（spec §9）。
 *
 * 覆盖「从引用打开原文」的四条路径：
 *   a) spans 未返回前先出 iframe（#page=N）；
 *   b) 非空 spans 到达后切换 PdfBboxViewer，iframe 卸载；
 *   c) 空 / 降级 / 请求失败保持 iframe；
 *   d) 被后一次 open 取代的旧 open，其迟到 spans 不得覆盖新文档。
 *
 * 本文件单独用 jsdom（仓库 vitest 默认 node 环境）。PdfBboxViewer 被替身化，
 * 避免把 pdf.js / canvas 拖进 jsdom。
 */
import React, { useEffect } from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('@jonex/shell-sdk', () => ({
  getShellContext: () => undefined,
  isPlatformAdmin: () => false,
}));

vi.mock('@/api/domainKnowledge', () => ({
  getDocumentViewTicket: vi.fn(),
  getDocumentSourceSpans: vi.fn(),
}));

// 替身只渲染一个可断言的标记，绕开 pdf.js / canvas。
vi.mock('@/components/PdfBboxViewer', async () => {
  const ReactModule = await import('react');
  return {
    PdfBboxViewer: ({ url, spans }: { url: string; spans: Array<{ bbox: number[] }> }) =>
      ReactModule.createElement('div', {
        'data-testid': 'bbox-viewer',
        'data-url': url,
        'data-bbox': (spans[0]?.bbox ?? []).join(','),
      }),
  };
});

import { getDocumentSourceSpans, getDocumentViewTicket } from '@/api/domainKnowledge';
import { useDocumentViewer } from '@/components/DocumentViewer';
import type { SourceSpan } from '@/components/bboxOverlay';

const mockTicket = vi.mocked(getDocumentViewTicket);
const mockSpans = vi.mocked(getDocumentSourceSpans);

interface SpansResponse {
  spans: SourceSpan[];
  degraded: boolean;
  reason?: string | null;
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

let viewerApi: ReturnType<typeof useDocumentViewer> | null = null;

function Harness() {
  const api = useDocumentViewer();
  useEffect(() => {
    viewerApi = api;
  }, [api]);
  return <div>{api.viewer}</div>;
}

function ticketFor(docId: string) {
  return Promise.resolve({
    url: `/api/v1/knowledge-base/documents/${docId}/raw?token=t`,
    expiresIn: 300,
  });
}

/** 把已 resolve / reject 的 promise 链排干（微任务 + 一个宏任务）。 */
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function iframe(): HTMLIFrameElement | null {
  return document.querySelector('iframe');
}

function bboxViewer(): HTMLElement | null {
  return document.querySelector('[data-testid="bbox-viewer"]');
}

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
  if (!window.ResizeObserver) {
    (window as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  // jsdom 对 getComputedStyle 的「伪元素」第二参直接抛 Not implemented；
  // antd Modal 的滚动条测量会带它。这里忽略第二参，避免整屏 stderr 噪音。
  const computedStyle = window.getComputedStyle.bind(window);
  window.getComputedStyle = ((el: Element) => computedStyle(el)) as typeof window.getComputedStyle;
});

beforeEach(() => {
  viewerApi = null;
  mockTicket.mockReset();
  mockSpans.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('DocumentViewer bbox decision', () => {
  it('a) renders the iframe with #page=N before spans resolve', async () => {
    const pending = deferred<SpansResponse>();
    mockTicket.mockImplementation(ticketFor);
    mockSpans.mockReturnValue(pending.promise);

    render(<Harness />);
    act(() => {
      viewerApi!.openDocument({ docId: 'docA', fileName: 'a.pdf', page: 3, chunkIndex: 7 });
    });

    await waitFor(() => {
      expect(iframe()).toBeTruthy();
    });
    expect(iframe()!.getAttribute('src')).toContain('#page=3');
    expect(bboxViewer()).toBeNull();

    // 收尾：让 4s 超时定时器被 finally 清掉。
    pending.resolve({ spans: [], degraded: false });
    await flush();
  });

  it('b) switches to the bbox viewer once non-empty spans resolve', async () => {
    const pending = deferred<SpansResponse>();
    mockTicket.mockImplementation(ticketFor);
    mockSpans.mockReturnValue(pending.promise);

    render(<Harness />);
    act(() => {
      viewerApi!.openDocument({ docId: 'docA', fileName: 'a.pdf', page: 3, chunkIndex: 7 });
    });
    await waitFor(() => expect(iframe()).toBeTruthy());

    pending.resolve({
      spans: [{ page_idx: 2, bbox: [10, 20, 30, 40], ordinal: 0 }],
      degraded: false,
    });
    await flush();

    await waitFor(() => {
      expect(bboxViewer()).toBeTruthy();
      expect(iframe()).toBeNull();
    });
    expect(bboxViewer()!.getAttribute('data-bbox')).toBe('10,20,30,40');
  });

  it.each([
    ['empty spans', { spans: [], degraded: false }],
    ['degraded spans', { spans: [], degraded: true, reason: 'engine_timeout' }],
  ])('c) keeps the iframe for %s', async (_name, response) => {
    mockTicket.mockImplementation(ticketFor);
    mockSpans.mockResolvedValue(response as SpansResponse);

    render(<Harness />);
    act(() => {
      viewerApi!.openDocument({ docId: 'docA', fileName: 'a.pdf', page: 3, chunkIndex: 7 });
    });
    await waitFor(() => expect(iframe()).toBeTruthy());

    await waitFor(() => expect(mockSpans).toHaveBeenCalled());
    await flush();

    expect(iframe()).toBeTruthy();
    expect(iframe()!.getAttribute('src')).toContain('#page=3');
    expect(bboxViewer()).toBeNull();
  });

  it('c) keeps the iframe when the spans request rejects', async () => {
    mockTicket.mockImplementation(ticketFor);
    mockSpans.mockRejectedValue(new Error('network down'));

    render(<Harness />);
    act(() => {
      viewerApi!.openDocument({ docId: 'docA', fileName: 'a.pdf', page: 3, chunkIndex: 7 });
    });
    await waitFor(() => expect(iframe()).toBeTruthy());

    await waitFor(() => expect(mockSpans).toHaveBeenCalled());
    await flush();

    expect(iframe()).toBeTruthy();
    expect(bboxViewer()).toBeNull();
  });

  it('d) a superseded open does not overwrite the newer document', async () => {
    const spansA = deferred<SpansResponse>();
    const spansB = deferred<SpansResponse>();
    mockTicket.mockImplementation(ticketFor);
    mockSpans.mockImplementation((docId: string) =>
      docId === 'docA' ? spansA.promise : spansB.promise,
    );

    render(<Harness />);
    act(() => {
      viewerApi!.openDocument({ docId: 'docA', fileName: 'a.pdf', page: 1, chunkIndex: 1 });
    });
    await waitFor(() => expect(iframe()!.getAttribute('src')).toContain('docA'));

    // docA 的 spans 还没返回就打开 docB。
    act(() => {
      viewerApi!.openDocument({ docId: 'docB', fileName: 'b.pdf', page: 2, chunkIndex: 2 });
    });
    await waitFor(() => expect(iframe()!.getAttribute('src')).toContain('docB'));

    spansB.resolve({
      spans: [{ page_idx: 1, bbox: [20, 20, 30, 30], ordinal: 0 }],
      degraded: false,
    });
    await flush();
    await waitFor(() => expect(bboxViewer()!.getAttribute('data-bbox')).toBe('20,20,30,30'));

    // docA 的迟到响应不得替换 docB 的 spans。
    spansA.resolve({
      spans: [{ page_idx: 0, bbox: [10, 10, 15, 15], ordinal: 0 }],
      degraded: false,
    });
    await flush();

    expect(bboxViewer()).toBeTruthy();
    expect(bboxViewer()!.getAttribute('data-bbox')).toBe('20,20,30,30');
    expect(bboxViewer()!.getAttribute('data-url')).toContain('docB');
  });
});
