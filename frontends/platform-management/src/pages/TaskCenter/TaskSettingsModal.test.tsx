// @vitest-environment jsdom
/**
 * 「任务设置」弹窗：只留手动执行（常开），没有开关、没有定时触发。
 *
 * 守两件事：触发区不再有可点的开关（后端还没有触发器接口，画一个开关就是骗人）；
 * 保存确实调用 `def.update` 并带上改过的名字（写不到后端就会「保存成功但名字没变」）。
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { TaskDefinitionItem } from '../../api/taskCenterDefinitions';

const updateDefinition = vi.fn();

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh-CN' } }),
}));

vi.mock('../../api/taskCenterDefinitions', () => ({
  updateDefinition: (...args: unknown[]) => updateDefinition(...args),
}));

import TaskSettingsModal from './TaskSettingsModal';

const task = (): TaskDefinitionItem => ({
  id: 'def-1',
  scope: 'space',
  space_id: 'space-1',
  name: '知识库文档解析',
  description: '旧描述',
  status: 'published',
  current_version_no: 1,
  execution_count: 0,
  last_execution_result: null,
});

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

beforeEach(() => {
  updateDefinition.mockReset();
  updateDefinition.mockResolvedValue({ definition: task(), versions: [], draft_version_no: null });
});

describe('任务设置弹窗', () => {
  it('触发配置只有手动执行，没有任何开关，也没有定时触发', () => {
    render(<TaskSettingsModal open task={task()} onCancel={() => {}} onSaved={() => {}} />);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.getByText('taskCenter.definition.settings.manual')).toBeTruthy();
    expect(screen.getByText('taskCenter.definition.settings.manualDefault')).toBeTruthy();
    expect(screen.queryByText(/定时/)).toBeNull();
    cleanup();
  });

  it('改完名字保存后调用 def.update，并回调解锁刷新', async () => {
    const onSaved = vi.fn();
    render(<TaskSettingsModal open task={task()} onCancel={() => {}} onSaved={onSaved} />);

    fireEvent.change(screen.getByLabelText('taskCenter.definition.column.name'), {
      target: { value: '改名后的任务' },
    });
    fireEvent.click(screen.getByText('common.save'));

    await waitFor(() => expect(updateDefinition).toHaveBeenCalledTimes(1));
    expect(updateDefinition).toHaveBeenCalledWith('def-1', {
      name: '改名后的任务',
      description: '旧描述',
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    cleanup();
  });

  it('保存失败不回调 onSaved，弹窗留在原地', async () => {
    updateDefinition.mockRejectedValueOnce(new Error('conflict'));
    const onSaved = vi.fn();
    render(<TaskSettingsModal open task={task()} onCancel={() => {}} onSaved={onSaved} />);

    fireEvent.click(screen.getByText('common.save'));

    await waitFor(() => expect(updateDefinition).toHaveBeenCalledTimes(1));
    expect(onSaved).not.toHaveBeenCalled();
    cleanup();
  });
});
