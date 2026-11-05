/**
 * 知识库创建/编辑弹窗的「结构化数据支持」开关（jsdom + RTL）。
 *
 * 钉死三件事：
 * - **创建默认关**：新建时不碰开关，`onOk` 载荷里的 `structured_enabled` 必须是布尔 `false`
 *   （不是 `undefined` —— 后端按布尔落库，缺省会被当成未传）。
 * - **可开启**：手动打开后载荷为 `true`。
 * - **编辑回填**：`editingKb.structuredEnabled` 为 true 时开关呈选中态。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名），不锁中文文案。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import CreateEditModal from './CreateEditModal';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string) => key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const baseProps = {
  open: true,
  submitting: false,
  onCancel: () => {},
};

/** 开关在 antd 里是 `<button role="switch">`，选中态看 aria-checked（未装 jest-dom，手读属性）。 */
function switchEl(): HTMLElement {
  return screen.getByRole('switch');
}

function isSwitchChecked(): boolean {
  return switchEl().getAttribute('aria-checked') === 'true';
}

describe('CreateEditModal structured toggle', () => {
  it('defaults to off and submits false on create', async () => {
    const onOk = vi.fn();
    render(<CreateEditModal {...baseProps} editingKb={null} onOk={onOk} />);

    expect(isSwitchChecked()).toBe(false);

    fireEvent.change(screen.getByPlaceholderText('domainKnowledge.namePlaceholder'), {
      target: { value: '文档库' },
    });
    fireEvent.click(screen.getByText('common.create'));

    await waitFor(() => expect(onOk).toHaveBeenCalledTimes(1));
    expect(onOk.mock.calls[0][0].structured_enabled).toBe(false);
  });

  it('submits true when the switch is turned on', async () => {
    const onOk = vi.fn();
    render(<CreateEditModal {...baseProps} editingKb={null} onOk={onOk} />);

    fireEvent.change(screen.getByPlaceholderText('domainKnowledge.namePlaceholder'), {
      target: { value: '数据库' },
    });
    fireEvent.click(switchEl());
    expect(isSwitchChecked()).toBe(true);

    fireEvent.click(screen.getByText('common.create'));

    await waitFor(() => expect(onOk).toHaveBeenCalledTimes(1));
    expect(onOk.mock.calls[0][0].structured_enabled).toBe(true);
  });

  it('backfills true for an enabled kb', () => {
    render(
      <CreateEditModal
        {...baseProps}
        editingKb={{ id: 'kb1', name: '数据库', structuredEnabled: true } as never}
        onOk={() => {}}
      />,
    );

    expect(isSwitchChecked()).toBe(true);
  });
});
