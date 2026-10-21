// @vitest-environment jsdom
/**
 * 「执行结果」面板的交互测试：点开、按引用渲染、跳转、收起。
 *
 * 这里测的是**渲染与跳转接线**，不是映射规则本身（那些在 `resultRefs.test.ts` 里）。
 * 重点守三条：只有成功的步骤才有入口；浮层里的入口真的能打开面板；产物链接真的走
 * `navigateCrossApp`（而不是把运行方的 API 路径塞进 `<a href>` —— 那样会丢 `Authorization` 头）。
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import type { SubtaskConsoleItem } from '../../api/taskCenter';
import SubtaskResult from './SubtaskResult';

const navigateCrossApp = vi.fn();
vi.mock('@jonex/shell-sdk', () => ({
  navigateCrossApp: (path: string) => navigateCrossApp(path),
}));

vi.mock('react-i18next', () => ({
  // 面板按当前语言选列表分隔符（中文顿号 / 英文逗号），mock 里也带上 i18n
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh-CN' } }),
}));

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
});

afterEach(() => {
  cleanup();
  navigateCrossApp.mockReset();
});

function makeItem(overrides: Partial<SubtaskConsoleItem> = {}): SubtaskConsoleItem {
  return {
    id: 'st-1',
    node_id: 'parse_1',
    subtask_name: '文件解析',
    status: 'success',
    error_message: null,
    started_at: null,
    finished_at: null,
    duration_seconds: 12,
    input_refs: [
      {
        kind: 'knowledge_base.document',
        params: { knowledge_base_id: 'kb-1', document_id: 'doc-1', document_name: '合规手册.pdf' },
      },
    ],
    output_refs: [{ kind: 'knowledge_base.chunks', params: { knowledge_base_id: 'kb-1', document_id: 'doc-1' } }],
    ...overrides,
  };
}

describe('SubtaskResult', () => {
  it('默认收起，点「执行结果」才展开面板', async () => {
    render(<SubtaskResult item={makeItem()} />);
    expect(screen.queryByText('taskCenter.execution.title')).toBeNull();

    fireEvent.click(screen.getByText('taskCenter.execution.trigger'));

    expect(await screen.findByText('taskCenter.execution.title')).toBeTruthy();
  });

  it('按引用渲染输入文件名，产物链接走跨应用跳转', async () => {
    render(<SubtaskResult item={makeItem()} />);
    fireEvent.click(screen.getByText('taskCenter.execution.trigger'));

    expect(await screen.findByText('合规手册.pdf')).toBeTruthy();

    fireEvent.click(screen.getByText('taskCenter.execution.viewChunks'));
    expect(navigateCrossApp).toHaveBeenCalledWith('/apps/core-business/domain-knowledge/kb-1/documents/doc-1/result');
  });

  it('非成功状态不渲染入口', () => {
    for (const status of ['pending', 'running', 'failed', 'timeout', 'stopped'] as const) {
      const { unmount } = render(<SubtaskResult item={makeItem({ status })} />);
      expect(screen.queryByText('taskCenter.execution.trigger')).toBeNull();
      unmount();
    }
  });

  it('成功但没有引用时显示空态，不编造数据', async () => {
    render(
      <SubtaskResult
        item={makeItem({ duration_seconds: null, input_refs: null, output_refs: null })}
      />,
    );
    fireEvent.click(screen.getByText('taskCenter.execution.trigger'));

    expect(await screen.findByText('taskCenter.execution.emptyInput')).toBeTruthy();
    expect(screen.getByText('taskCenter.execution.emptyOutput')).toBeTruthy();
  });

  it('关闭按钮收起面板', async () => {
    render(<SubtaskResult item={makeItem()} />);
    fireEvent.click(screen.getByText('taskCenter.execution.trigger'));
    const title = await screen.findByText('taskCenter.execution.title');
    const popover = title.closest('.ant-popover');
    expect(popover).toBeTruthy();

    fireEvent.click(screen.getByLabelText('taskCenter.execution.close'));

    // antd 收起时保留 DOM、只挂隐藏类，所以判类名而不是判「节点不存在」
    await waitFor(() => expect(popover?.className).toContain('ant-popover-hidden'));
  });
});
