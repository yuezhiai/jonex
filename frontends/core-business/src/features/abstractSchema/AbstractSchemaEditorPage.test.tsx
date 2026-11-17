/**
 * 抽象 schema 编辑器：空态入口 + 保存历史 Drawer（列表 / 空 / 错 / 预览 / 还原 / 冲突）。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名，带参数则附 JSON），
 * 不锁中文文案。antd 静态 message 用 spy 断言，避免依赖 holder 的渲染时序。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Modal, message } from 'antd';

const {
  tMock, genMock, getMock, listHistoryMock, historyDetailMock, restoreMock,
  startRunMock, getRunMock,
} = vi.hoisted(() => ({
  // 进度条文案走真实 i18n key + 插值：这里把 done/total 拼进返回值，
  // 便于断言页面出现「3/10」，而不是只锁 key。
  tMock: (key: string, params?: Record<string, unknown>) =>
    key === 'abstractSchema.landingAgent.progress' && params
      ? `${key} ${params.done}/${params.total} ${params.phase}`
      : params ? `${key}(${JSON.stringify(params)})` : key,
  genMock: vi.fn(),
  getMock: vi.fn(),
  listHistoryMock: vi.fn(),
  historyDetailMock: vi.fn(),
  restoreMock: vi.fn(),
  startRunMock: vi.fn(),
  getRunMock: vi.fn(),
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
vi.mock('@/api/landingAgent', () => ({
  startLandingRun: startRunMock,
  getLandingRun: getRunMock,
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
  vi.useRealTimers();
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
  it('点击「生成概念候选」打开弹窗，后台入口不可用时回退到「文档本体 + 自动落点」', async () => {
    getMock.mockResolvedValue(null);
    // 开关关闭 / 参数无效 = 1001。`postData` 把信封 code 放进 `bizCode`
    // （Error 实例），组件只认 `bizCode` —— 这里就用这个真实形状。
    startRunMock.mockRejectedValue(Object.assign(new Error('AI 落点未启用'), { bizCode: 1001 }));
    genMock.mockResolvedValue({ schema: { entities: [{}, {}] } });
    const infoSpy = vi.spyOn(message, 'info').mockReturnValue(undefined as never);

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);

    await openGenerateModal();
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));

    // 空态没有草稿 → run_mode 只能是 full。
    await waitFor(() => expect(startRunMock).toHaveBeenCalledWith('kb_1', 'full', { aliasHints: '', force: false }));
    // 生成默认必须走「复用文档本体 + 自动落点」：结构化聚类词面（泛化属性名、缺业务别名）
    // 会让检索词面命中不到、或被同库多实体歧义挡住 —— 这是两次重建 demo 库后的教训。
    await waitFor(() => expect(genMock).toHaveBeenCalledWith('kb_1', false, undefined, {
      seed: 'document',
      autoLandings: true,
      aliasHints: '',
    }));
    expect(infoSpy).toHaveBeenCalledWith('abstractSchema.landingAgent.fallbackHint');
  });

  it('回退路径：弹窗里填的业务词表会原样传给生成（客户端去空白）', async () => {
    getMock.mockResolvedValue(null);
    startRunMock.mockRejectedValue(Object.assign(new Error('AI 落点未启用'), { bizCode: 1001 }));
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

describe('AbstractSchemaEditorPage 落点后台生成', () => {
  const runningProgress = {
    conceptsTotal: 10,
    conceptsDone: 3,
    landed: 2,
    unresolved: 1,
    lowConfidence: 0,
    toolCalls: 9,
    tokens: 0,
  };

  beforeEach(() => {
    getMock.mockResolvedValue(schemaFixture);
    useEditorStore.setState({ dirty: false });
  });

  async function confirmGenerate() {
    await openGenerateModal();
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));
  }

  it('有草稿时默认提交 landings 模式', async () => {
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({ runId: 'r1', status: 'running', phase: 'judge', progress: runningProgress });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    await waitFor(() => expect(startRunMock).toHaveBeenCalledWith('kb_1', 'landings', { aliasHints: '', force: false }));
  });

  it('取消弹窗会重置「全部重来」勾选，不粘到下一次', async () => {
    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await openGenerateModal();
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'common.cancel' }));
    await waitFor(() => expect(screen.queryByRole('checkbox')).toBeNull());

    fireEvent.click(await screen.findByRole('button', { name: /abstractSchema\.generate/ }));
    const box = await screen.findByRole('checkbox') as HTMLInputElement;
    expect(box.checked).toBe(false);
  });

  it('草稿存在但没有实体时默认 full（空 landings 会空跑一遍）', async () => {
    getMock.mockResolvedValue({
      ...schemaFixture,
      schema: { entities: [], relations: [], metrics: [] },
    });
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({
      runId: 'r1', status: 'running', phase: 'recall',
      progress: { ...runningProgress, conceptsTotal: 0, conceptsDone: 0 },
    });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    await waitFor(() => expect(startRunMock).toHaveBeenCalledWith('kb_1', 'full', { aliasHints: '', force: false }));
  });

  it('勾选「全部重来」后提交 full 模式', async () => {
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({ runId: 'r1', status: 'running', phase: 'recall', progress: runningProgress });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await openGenerateModal();
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));

    await waitFor(() => expect(startRunMock).toHaveBeenCalledWith('kb_1', 'full', { aliasHints: '', force: false }));
  });

  it('运行中显示「已处理 n/总数」，成功后自动刷新页面', async () => {
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock
      .mockResolvedValueOnce({ runId: 'r1', status: 'running', phase: 'judge', progress: runningProgress })
      .mockResolvedValue({
        runId: 'r1',
        status: 'succeeded',
        phase: 'done',
        progress: { ...runningProgress, conceptsDone: 10, unresolved: 8, landed: 2 },
        summary: { schema_version: 8 },
      });
    const successSpy = vi.spyOn(message, 'success').mockReturnValue(undefined as never);

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    expect(await screen.findByText(/3\/10/)).toBeTruthy();
    // 终态后 load() 重拉当前 schema（首次挂载 1 次 + 完成后 1 次）
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2), { timeout: 6000 });
    expect(successSpy).toHaveBeenCalledWith('abstractSchema.landingAgent.succeeded');
  });

  it('同库已有 run 在飞（429）→ 提示正在生成中', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    startRunMock.mockRejectedValue({ status: 429, bizCode: 3007 });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('abstractSchema.landingAgent.busy'));
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it('run 失败 → 透出错误并停止轮询，不刷新页面', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({
      runId: 'r1', status: 'failed', phase: 'done', progress: runningProgress, error: 'boom',
    });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('boom'));
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it('轮询遇到 1002/404 视为终态：提示后端 message 并停止', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockRejectedValue(
      Object.assign(new Error('运行不存在'), { status: 404, bizCode: 1002 }),
    );

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await confirmGenerate();

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('运行不存在'));
    // 终态后不再排下一拍（2s 后再看一次，仍只有首拉）
    await new Promise((resolve) => setTimeout(resolve, 2200));
    expect(getRunMock).toHaveBeenCalledTimes(1);
    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it('重复提交（双击确认）时后一次生成接管，前一次不再轮询', async () => {
    let resolveFirst!: (value: { runId: string; status: string }) => void;
    const firstStart = new Promise<{ runId: string; status: string }>((resolve) => {
      resolveFirst = resolve;
    });
    startRunMock
      .mockReturnValueOnce(firstStart)
      .mockResolvedValueOnce({ runId: 'r2', status: 'queued' });
    getRunMock.mockResolvedValue({
      runId: 'r2', status: 'running', phase: 'judge', progress: runningProgress,
    });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    await openGenerateModal();
    const ok = await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' });
    // 同一个 act 内连点两次（弹窗卸载前的双击路径）
    await act(async () => {
      ok.click();
      ok.click();
    });

    // 第二次生成已建单并开始轮询 r2
    await waitFor(() => expect(
      getRunMock.mock.calls.some(([, runId]) => runId === 'r2'),
    ).toBe(true));

    // 第一次的 start 晚到：代次已变，必须作废（不建定时器、不轮询 r1）
    await act(async () => { resolveFirst({ runId: 'r1', status: 'queued' }); });
    await new Promise((resolve) => setTimeout(resolve, 2200));
    expect(getRunMock.mock.calls.filter(([, runId]) => runId === 'r1')).toHaveLength(0);
  });

  it('组件卸载后不再轮询（假定时器）', async () => {
    vi.useFakeTimers();
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({ runId: 'r1', status: 'running', phase: 'judge', progress: runningProgress });

    const view = render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    // 初始 load（getAbstractSchema）落定
    await act(async () => { await Promise.resolve(); });

    fireEvent.click(screen.getByRole('button', { name: /abstractSchema\.generate/ }));
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));
    // start 返回 + 首拉（轮询链的第一拍）
    await act(async () => { await Promise.resolve(); });
    expect(getRunMock).toHaveBeenCalledTimes(1);

    view.unmount();
    await act(async () => { vi.advanceTimersByTime(5000); });

    // 卸载时已清掉自调度的 setTimeout：不会再有轮询
    expect(getRunMock).toHaveBeenCalledTimes(1);
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

describe('AbstractSchemaEditorPage 落点模式守卫', () => {
  it('有草稿（landings 模式）时业务词表输入禁用', async () => {
    getMock.mockResolvedValue(schemaFixture);
    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    const textarea = (await openGenerateModal()) as HTMLTextAreaElement;
    // landings-only 不重造概念/别名，词表在本模式没有作用域。
    expect(textarea.disabled).toBe(true);
  });

  it('已确认（active）时生成按钮禁用（与 readOnly 同一道闸）', async () => {
    getMock.mockResolvedValue({ ...schemaFixture, status: 'active' });
    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    const btn = await screen.findByRole('button', { name: /abstractSchema\.generate/ });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it('queued 阶段 phase 为空时进度不渲染悬挂分隔符', async () => {
    getMock.mockResolvedValue(schemaFixture);
    startRunMock.mockResolvedValue({ runId: 'r1', status: 'queued' });
    getRunMock.mockResolvedValue({
      runId: 'r1', status: 'running', phase: '',
      progress: {
        phase: '', conceptsTotal: 4, conceptsDone: 0, landed: 0,
        unresolved: 0, lowConfidence: 0, toolCalls: 0, tokens: 0,
      },
    });

    render(<AbstractSchemaEditorPage kbId="kb_1" canWrite />);
    fireEvent.click(await screen.findByRole('button', { name: /abstractSchema\.generate/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'abstractSchema.aliasHints.confirm' }));

    await waitFor(() => expect(screen.getByText(/landingAgent\.progressOnly/)).toBeTruthy());
    expect(screen.queryByText(/ · /)).toBeNull();
  });
});
