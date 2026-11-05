/**
 * 「聚焦生成」弹窗的组件级回归（jsdom + RTL）。
 *
 * 钉死两件事：
 * - **B1 双义文案**：同为 1001 的两支错误（`focus_empty`「没有解析出相关实体」与
 *   `no_source_schema`「没有任何已生成 schema 的数据源」）必须靠**服务端 message
 *   原文**区分，绝不能按 1001 特判回落成本地键 —— 否则后者会被误显示成前者。
 * - **解析成功后的第二步渲染**：种子可删、删光后「确认生成」禁用（不靠运气兜底）。
 *
 * 断言一律按 **i18n key**（`react-i18next` 被 mock 成直出键名），不锁中文文案 ——
 * 服务端 message 原文例外（那正是被测对象本身）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import FocusGenerateModal from './FocusGenerateModal';
import { previewFocusAbstractSchema } from '@/api/abstractSchema';

// useTranslation 必须返回**逐次渲染恒等**的 t：组件用它做 useCallback/useEffect 依赖，
// 每次渲染都给新函数会造成 effect 无限重跑（mock 与真实实现的行为差异，必须消掉）。
const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/abstractSchema', () => ({
  previewFocusAbstractSchema: vi.fn(),
}));

const previewMock = vi.mocked(previewFocusAbstractSchema);

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function renderModal() {
  const props = {
    open: true,
    kbId: 'kb_1',
    generating: false,
    onClose: vi.fn(),
    onGenerate: vi.fn(async () => true),
  };
  render(<FocusGenerateModal {...props} />);
  return props;
}

async function fillPromptAndParse(prompt: string) {
  fireEvent.change(screen.getByPlaceholderText('abstractSchema.focus.promptPlaceholder'), {
    target: { value: prompt },
  });
  fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.focus.parse' }));
}

describe('FocusGenerateModal', () => {
  it('A1：preview 失败（no_source_schema）→ Alert 显示服务端 message 原文，不回落本地键', async () => {
    const serverMessage = '该知识库下没有任何已生成 schema 的数据源，无法提取抽象概念';
    previewMock.mockRejectedValueOnce(new Error(serverMessage));

    renderModal();
    await fillPromptAndParse('招聘相关实体');

    expect(await screen.findByText(serverMessage)).toBeTruthy();
    expect(screen.queryByText('abstractSchema.focus.parseFailed')).toBeNull();
  });

  it('A2：preview 失败（focus_empty）→ 同样透出服务端原文，与 A1 可区分', async () => {
    const serverMessage = '没有解析出与提示词相关的实体，请调整提示词';
    previewMock.mockRejectedValueOnce(new Error(serverMessage));

    renderModal();
    await fillPromptAndParse('不存在的概念');

    expect(await screen.findByText(serverMessage)).toBeTruthy();
    // 两支 1001 的区分正在于「显示哪段 message」——不许退化成同一个本地键
    expect(screen.queryByText('abstractSchema.focus.parseFailed')).toBeNull();
  });

  it('A3：preview 失败且 Error 无 message → 回落本地键 abstractSchema.focus.parseFailed', async () => {
    previewMock.mockRejectedValueOnce(new Error());

    renderModal();
    await fillPromptAndParse('招聘相关实体');

    expect(await screen.findByText('abstractSchema.focus.parseFailed')).toBeTruthy();
  });

  it('A4：解析成功 → 第二步渲染种子；删光种子后「确认生成」禁用', async () => {
    previewMock.mockResolvedValueOnce({
      seeds: [
        { dataSourceId: 'ds_a', entityName: 'Job', sourceName: '招聘库', role: 'focus' },
        { dataSourceId: 'ds_b', entityName: 'Employee', sourceName: '资产库', role: 'partner' },
      ],
      neighbors: [],
      warnings: [],
    });

    renderModal();
    await fillPromptAndParse('招聘相关实体');

    // 第二步：种子实体名出现，footer 换成「重新解析 / 确认生成」
    expect(await screen.findByText('Job')).toBeTruthy();
    expect(screen.getByText('Employee')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'abstractSchema.focus.reparse' })).toBeTruthy();
    const confirmBtn = screen.getByRole('button', {
      name: 'abstractSchema.focus.confirm',
    }) as HTMLButtonElement;
    expect(confirmBtn.disabled).toBe(false);
    // 第一步的「解析」按钮已退场（footer 按 step 切换）
    expect(screen.queryByRole('button', { name: 'abstractSchema.focus.parse' })).toBeNull();

    // 逐枚点掉 ×（每次重新查询：删一枚会整表重渲染，别攒着旧引用）
    fireEvent.click(screen.getAllByRole('button', { name: '×' })[0]);
    fireEvent.click(screen.getAllByRole('button', { name: '×' })[0]);

    expect(screen.getByText('abstractSchema.focus.needSeed')).toBeTruthy();
    expect(confirmBtn.disabled).toBe(true);
  });
});
