/** 导入 TBox 弹窗：预览（dry_run）→ 摘要展示 → 确认导入 → onImported/onClose。 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import { ImportTboxModal } from './ImportTboxModal';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    (params ? `${key}(${JSON.stringify(params)})` : key),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));

const importAbstractSchema = vi.hoisted(() => vi.fn());
vi.mock('@/api/abstractSchema', () => ({ importAbstractSchema }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ImportTboxModal', () => {
  it('预览 → 摘要 → 确认导入（dry_run 先、正式后；首次导入版本传 0）', async () => {
    importAbstractSchema
      .mockResolvedValueOnce({
        dryRun: true,
        summary: { entitiesAdded: 2, entitiesUpdated: 1, attributesAdded: 3,
                   ignoredSections: ['constraints(1)'] },
        warnings: ['关系 R 的端点不在实体集里（含已有实体），已跳过'],
        schema: null,
      })
      .mockResolvedValueOnce({
        dryRun: false,
        summary: { entitiesAdded: 2, entitiesUpdated: 1, attributesAdded: 3,
                   ignoredSections: ['constraints(1)'] },
        warnings: [],
        schema: { id: 'row', schemaVersion: 8 },
      });
    const onImported = vi.fn();
    const onClose = vi.fn();
    render(<ImportTboxModal kbId="kb_1" expectedSchemaVersion={0} open
                            onClose={onClose} onImported={onImported} />);

    fireEvent.change(screen.getByPlaceholderText('abstractEditor.importYamlPlaceholder'),
                     { target: { value: 'entity_types: []\n' } });
    fireEvent.click(screen.getByText('abstractEditor.importPreview'));
    // 摘要出现（含忽略段与告警）
    expect(await screen.findByText(/abstractEditor.importSummary/)).toBeTruthy();
    expect(screen.getByText(/constraints\(1\)/)).toBeTruthy();
    expect(importAbstractSchema).toHaveBeenNthCalledWith(1, 'kb_1', {
      yamlText: 'entity_types: []\n', expectedSchemaVersion: 0, dryRun: true,
    });

    fireEvent.click(screen.getByText('abstractEditor.importConfirm'));
    expect(importAbstractSchema).toHaveBeenNthCalledWith(2, 'kb_1', {
      yamlText: 'entity_types: []\n', expectedSchemaVersion: 0, dryRun: false,
    });
    // 成功回调（异步收尾）
    await vi.waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('文本改动 → 预览作废（确认按钮禁用）', async () => {
    importAbstractSchema.mockResolvedValue({
      dryRun: true, summary: { entitiesAdded: 0, entitiesUpdated: 0,
                               attributesAdded: 0, ignoredSections: [] },
      warnings: [], schema: null,
    });
    render(<ImportTboxModal kbId="kb_1" expectedSchemaVersion={5} open
                            onClose={vi.fn()} onImported={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText('abstractEditor.importYamlPlaceholder'),
                     { target: { value: 'a: 1\n' } });
    fireEvent.click(screen.getByText('abstractEditor.importPreview'));
    await screen.findByText(/abstractEditor.importSummary/);

    fireEvent.change(screen.getByPlaceholderText('abstractEditor.importYamlPlaceholder'),
                     { target: { value: 'a: 2\n' } });
    const confirm = screen.getByText('abstractEditor.importConfirm').closest('button')!;
    expect(confirm.disabled).toBe(true);
  });

  it('confirmDirty 返回 false → 放弃导入（不写库、不回调）', async () => {
    importAbstractSchema.mockResolvedValue({
      dryRun: true, summary: { entitiesAdded: 1, entitiesUpdated: 0,
                               attributesAdded: 0, ignoredSections: [] },
      warnings: [], schema: null,
    });
    const onImported = vi.fn();
    const onClose = vi.fn();
    const confirmDirty = vi.fn(() => false);
    render(<ImportTboxModal kbId="kb_1" expectedSchemaVersion={7} open
                            onClose={onClose} onImported={onImported}
                            confirmDirty={confirmDirty} />);

    fireEvent.change(screen.getByPlaceholderText('abstractEditor.importYamlPlaceholder'),
                     { target: { value: 'a: 1\n' } });
    fireEvent.click(screen.getByText('abstractEditor.importPreview'));
    await screen.findByText(/abstractEditor.importSummary/);

    fireEvent.click(screen.getByText('abstractEditor.importConfirm'));
    // 用户放弃 → 正式写入一次都不发（只剩预览那次 dry_run）
    expect(confirmDirty).toHaveBeenCalledTimes(1);
    expect(importAbstractSchema).toHaveBeenCalledTimes(1);
    expect(importAbstractSchema).toHaveBeenNthCalledWith(1, 'kb_1', {
      yamlText: 'a: 1\n', expectedSchemaVersion: 7, dryRun: true,
    });
    expect(onImported).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
