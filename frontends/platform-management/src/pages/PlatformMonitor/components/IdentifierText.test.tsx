// @vitest-environment jsdom
/**
 * 标识单元格：可见文本是释义，原始 ID 只出现在 hover（title）与复制入口。
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import IdentifierText, { IdentifierWithProtocols } from './IdentifierText';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => {
      if (key.endsWith('.copy')) return '复制';
      if (key.endsWith('.copied')) return '已复制';
      // callProtocol.invoke / callProtocol.mcp → 取最后一段，模拟真实语言包
      if (key.startsWith('platformMonitor.callProtocol.')) return key.split('.').pop();
      return key;
    },
  }),
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
});

afterEach(() => cleanup());

describe('IdentifierText', () => {
  it('显示释义，原始 ID 在 title 与复制入口里', () => {
    const { container } = render(
      <IdentifierText value="business.task_center.v1" label="任务中心" strong />,
    );

    expect(screen.getByText('任务中心')).toBeTruthy();
    // 主要可见区不出现原始 ID
    expect(screen.queryByText('business.task_center.v1')).toBeNull();

    const titleNode = container.querySelector('[title="business.task_center.v1"]');
    expect(titleNode).toBeTruthy();
    expect(titleNode?.textContent).toContain('任务中心');
    // antd Typography 的复制按钮：复制内容必须是原始值
    expect(container.querySelector('.ant-typography-copy')).toBeTruthy();
  });

  it('未知能力 / 模型名照样用原值做可见文本，仍保留复制入口', () => {
    const { container } = render(<IdentifierText value="deepseek-v4-flash-202605" label="deepseek-v4-flash-202605" />);

    expect(screen.getByText('deepseek-v4-flash-202605')).toBeTruthy();
    expect(container.querySelector('[title="deepseek-v4-flash-202605"]')).toBeTruthy();
    expect(container.querySelector('.ant-typography-copy')).toBeTruthy();
  });

  it('空值显示占位符，不渲染复制入口', () => {
    const { container } = render(<IdentifierText value={null} label="-" />);

    expect(screen.getByText('-')).toBeTruthy();
    expect(container.querySelector('.ant-typography-copy')).toBeNull();
  });
});

describe('IdentifierWithProtocols', () => {
  it('同一展示名多协议时名称旁追加协议标签', () => {
    render(
      <IdentifierWithProtocols
        value="business.knowledge_base.v1"
        label="知识库"
        protocols={['invoke', 'mcp']}
      />,
    );

    expect(screen.getByText('知识库')).toBeTruthy();
    expect(screen.getByText('invoke')).toBeTruthy();
    expect(screen.getByText('mcp')).toBeTruthy();
  });

  it('单协议不传标签时不渲染协议', () => {
    const { container } = render(
      <IdentifierWithProtocols value="business.task_center.v1" label="任务中心" protocols={[]} />,
    );

    expect(screen.getByText('任务中心')).toBeTruthy();
    expect(container.querySelector('.yx-pm-protocol-tag')).toBeNull();
  });
});
