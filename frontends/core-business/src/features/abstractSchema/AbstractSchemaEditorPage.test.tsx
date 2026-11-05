/**
 * 抽象 schema 编辑器：空态入口 + 保存历史 Drawer（列表 / 空 / 错 / 预览 / 还原 / 冲突）。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名，带参数则附 JSON），
 * 不锁中文文案。antd 静态 message 用 spy 断言，避免依赖 holder 的渲染时序。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Modal, message } from 'antd';

const { tMock, genMock, getMock, listHistoryMock, historyDetailMock, restoreMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
  genMock: vi.fn(),
  getMock: vi.fn(),
  listHistoryMock: vi.fn(),
  historyDetailMock: vi.fn(),
  restoreMock: vi.fn(),
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock('@/api/abstractSchema', () => ({
  getAbstractSchema: getMock,
  generateAbstractSchema: genMock,
  saveAbstractSchema: vi.fn(),
  listAbstractSchemaHistory: listHistoryMock,
  getAbstractSchemaHistoryDetail: historyDetailMock,
  restoreAbstractSchemaHistory: restoreMock,
}));

import { AbstractSchemaEditorPage } from './AbstractSchemaEditorPage';
import { useEditorStore } from '../ontologyCanvas/store/editorStore.js';
import type {
  AbstractSchema,
  AbstractSchemaHistoryDetail,
  AbstractSchemaHistoryItem,
} from '@/api/abstractSchema';

const entity = (name: string) => ({
  name,
  description: '',
  aliases: [],
  sources: [],
  attributes: [],
});

const schemaFixture: AbstractSchema = {
  id: 'as_1',
  knowledgeBaseId: 'kb_1',
  schemaVersion: 7,
  status: 'proposed',
  schema: {
    entities: [entity('Job')],
    relations: [],
    metrics: [],
  },
  staleCount: 0,
};

const historyItems: AbstractSchemaHistoryItem[] = [
  {
    id: 's1', schemaVersion: 6, status: 'proposed', savedBy: 'alice',
    source: 'manual', createdAt: '2026-10-27T10:00:00Z',
  },
  {
    id: 's2', schemaVersion: 5, status: 'active', savedBy: null,
    source: 'restore', createdAt: null,
  },
];

const detailFixture: AbstractSchemaHistoryDetail = {
  id: 's1', schemaVersion: 6, status: 'proposed', savedBy: 'alice',
  source: 'manual', createdAt: '2026-10-27T10:00:00Z',
  knowledgeBaseId: 'kb_1',
  schema: {
    entities: [entity('Alpha'), entity('Beta')],
    relations: [{ name: 'R', source: 'Alpha', target: 'Beta', description: '', backings: [] }],
    metrics: [{ name: 'm1', aliases: [], additivity: 'unknown', landings: [] }],
  },
};

afterEach(() => {
  cleanup();
  // `Modal.confirm` 是独立 React root（挂在 document.body），RTL cleanup 收不掉；
  // 不销毁会让下一个用例的 findByText 命中多条重复标题。
  Modal.destroyAll();
  vi.restoreAllMocks();
  vi.clearAllMocks();
  message.destroy();
});

async function openGenerateModal() {
  const btn = await screen.findByRole('button', { name: /abstractSchema\.generate/ });
  fireEvent.click(btn);
  return screen.findByRole('textbox');
}

describe('AbstractSchemaEditorPage 空态', () => {
  it('点击「生成概念候选」打开弹窗，确认后按「文档本体 + 自动落点」生成', async () => {
    getMock.mockResolvedValue(null);
    genMock.mockResolvedValue({ schema: { entities: [{}, {}] } });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);

    await openGenerateModal();
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));

    // 生成默认必须走「复用文档本体 + 自动落点」：结构化聚类词面（泛化属性名、缺业务别名）
    // 会让检索词面命中不到、或被同库多实体歧义挡住 —— 这是两次重建 demo 库后的教训。
    await waitFor(() => expect(genMock).toHaveBeenCalledWith('kb_1', false, undefined, {
      seed: 'document',
      autoLandings: true,
      aliasHints: '',
    }));
  });

  it('弹窗里填的业务词表会原样传给生成（客户端去空白）', async () => {
    getMock.mockResolvedValue(null);
    genMock.mockResolvedValue({ schema: { entities: [] } });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);

    const textarea = await openGenerateModal();
    fireEvent.change(textarea, {
      target: { value: '集团汇总,跨厂对标 => DwdM12PlantDaily\n吨发,M12 => m12_daily  ' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));

    await waitFor(() => expect(genMock).toHaveBeenCalledWith('kb_1', false, undefined, {
      seed: 'document',
      autoLandings: true,
      aliasHints: '集团汇总,跨厂对标 => DwdM12PlantDaily\n吨发,M12 => m12_daily',
    }));
  });
});

describe('AbstractSchemaEditorPage 保存历史', () => {
  beforeEach(() => {
    getMock.mockResolvedValue(schemaFixture);
    listHistoryMock.mockResolvedValue({ items: historyItems, total: historyItems.length });
    // 画布 store 是全局单例；上一条用例置的 dirty 不带进下一条。
    useEditorStore.setState({ dirty: false });
  });

  async function openHistory(canWrite = true) {
    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite={canWrite} />);
    const btn = await screen.findByRole('button', { name: /abstractEditor\.history\.button/ });
    fireEvent.click(btn);
    await waitFor(() => expect(listHistoryMock).toHaveBeenCalledTimes(1));
  }

  it('打开 Drawer 拉取并渲染版本 / 来源 / 保存人', async () => {
    await openHistory();

    expect(listHistoryMock).toHaveBeenCalledWith('kb_1');
    expect(await screen.findByText('alice')).toBeTruthy();
    expect(screen.getByText('v6')).toBeTruthy();
    expect(screen.getByText('abstractEditor.history.sourceManual')).toBeTruthy();
    expect(screen.getByText('abstractEditor.history.sourceRestore')).toBeTruthy();
    // 无保存人显示占位，不渲染成 "null"
    expect(screen.queryByText('null')).toBeNull();
  });

  it('空列表显示空态文案', async () => {
    listHistoryMock.mockResolvedValue({ items: [], total: 0 });

    await openHistory();

    expect(await screen.findByText('abstractEditor.history.empty')).toBeTruthy();
  });

  it('列表加载失败显示错误 + 重试；重试成功后渲染列表', async () => {
    listHistoryMock
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue({ items: historyItems, total: historyItems.length });

    await openHistory();

    expect(await screen.findByText('boom')).toBeTruthy();
    expect(listHistoryMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));

    expect(await screen.findByText('alice')).toBeTruthy();
    expect(listHistoryMock).toHaveBeenCalledTimes(2);
  });

  it('预览拉详情并展示实体 / 关系 / 指标数量与实体名（不 dump JSON）', async () => {
    historyDetailMock.mockResolvedValue(detailFixture);

    await openHistory();
    const previewButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.preview',
    });
    fireEvent.click(previewButtons[0]!);

    expect(await screen.findByText(
      'abstractEditor.history.previewCounts({"entities":2,"relations":1,"metrics":1})',
    )).toBeTruthy();
    expect(screen.getByText('Alpha')).toBeTruthy();
    expect(screen.getByText('Beta')).toBeTruthy();
    expect(historyDetailMock).toHaveBeenCalledWith('s1', 'kb_1');
  });

  it('还原：二次确认后带「当前版本」调 API，成功后刷新页面数据与列表', async () => {
    const successSpy = vi.spyOn(message, 'success').mockReturnValue(undefined as never);
    restoreMock.mockResolvedValue({ ...schemaFixture, schemaVersion: 8 });

    await openHistory();
    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    fireEvent.click(restoreButtons[0]!);

    // antd Modal.confirm 把标题同时渲染进 aria 标题与可见标题 → 用 AllBy
    expect(await screen.findAllByText('abstractEditor.history.restoreTitle')).not.toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'abstractEditor.history.restoreOk' }));

    await waitFor(() => expect(restoreMock).toHaveBeenCalledWith('s1', 'kb_1', 7));
    // 成功 = load() 重拉当前 schema + 列表刷新
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(listHistoryMock).toHaveBeenCalledTimes(2));
    expect(successSpy).toHaveBeenCalledWith(
      'abstractEditor.history.restoreSuccess({"version":6})',
    );
  });

  it('还原冲突（1003）：提示刷新后重试并刷新列表，不静默重拉页面', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    restoreMock.mockRejectedValueOnce({ status: 409, bizCode: 1003 });

    await openHistory();
    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    fireEvent.click(restoreButtons[0]!);
    await screen.findAllByText('abstractEditor.history.restoreTitle');
    fireEvent.click(screen.getByRole('button', { name: 'abstractEditor.history.restoreOk' }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith(
      'abstractEditor.history.restoreConflict',
    ));
    await waitFor(() => expect(listHistoryMock).toHaveBeenCalledTimes(2));
    // 冲突时不重拉页面 schema：避免静默丢弃画布上未保存的编辑
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it('只读（!canWrite）时还原按钮禁用，预览仍可用', async () => {
    historyDetailMock.mockResolvedValue(detailFixture);

    await openHistory(false);

    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    restoreButtons.forEach((btn) => {
      expect((btn as HTMLButtonElement).disabled).toBe(true);
    });

    const previewButtons = screen.getAllByRole('button', {
      name: 'abstractEditor.history.preview',
    });
    expect((previewButtons[0] as HTMLButtonElement).disabled).toBe(false);
  });

  it('Drawer 刷新按钮重新拉列表', async () => {
    await openHistory();

    fireEvent.click(screen.getByTestId('history-refresh'));

    await waitFor(() => expect(listHistoryMock).toHaveBeenCalledTimes(2));
  });

  it('预览失败：透出后端 message', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    historyDetailMock.mockRejectedValueOnce(new Error('detail boom'));

    await openHistory();
    const previewButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.preview',
    });
    fireEvent.click(previewButtons[0]!);

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('detail boom'));
    expect(historyDetailMock).toHaveBeenCalledTimes(1);
  });

  it('还原非冲突失败：透出后端 message，不重拉页面/列表', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    restoreMock.mockRejectedValueOnce(new Error('save boom'));

    await openHistory();
    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    fireEvent.click(restoreButtons[0]!);
    await screen.findAllByText('abstractEditor.history.restoreTitle');
    fireEvent.click(screen.getByRole('button', { name: 'abstractEditor.history.restoreOk' }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('save boom'));
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(listHistoryMock).toHaveBeenCalledTimes(1);
  });

  it('还原确认框展示快照状态；快照状态与当前不同时追加状态变化警告', async () => {
    await openHistory();

    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    // s2 = active，当前 schema = proposed → 状态会变
    fireEvent.click(restoreButtons[1]!);

    expect(await screen.findByText(/abstractEditor\.history\.restoreSnapshotStatus/)).toBeTruthy();
    expect(
      await screen.findByText(/abstractEditor\.history\.restoreStatusChangeWarning/),
    ).toBeTruthy();
  });

  it('快照 proposed、当前 active：确认框明确提示将变草稿并退出检索', async () => {
    getMock.mockResolvedValue({ ...schemaFixture, status: 'active' });
    await openHistory();

    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    // s1 = proposed，当前 schema = active → 还原会退出检索
    fireEvent.click(restoreButtons[0]!);

    expect(
      await screen.findByText('abstractEditor.history.restoreDeactivateWarning'),
    ).toBeTruthy();
  });

  it('有未保存编辑时，还原确认框追加丢弃警告', async () => {
    await openHistory();
    act(() => { useEditorStore.setState({ dirty: true }); });

    const restoreButtons = await screen.findAllByRole('button', {
      name: 'abstractEditor.history.restore',
    });
    fireEvent.click(restoreButtons[0]!);

    expect(await screen.findByText('abstractEditor.history.restoreDirtyWarning')).toBeTruthy();
  });
});
