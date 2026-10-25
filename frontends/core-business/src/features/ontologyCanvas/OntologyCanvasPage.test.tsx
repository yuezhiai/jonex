/**
 * 「DS 级画布回落读到抽象投影」的前端防洗行护栏（组件级，jsdom + RTL）。
 *
 * 背景（p12 §三.1）：知识库存在 active 抽象 schema 时，`get_compiled_schema(ds=None)`
 * 返回的是**抽象投影**（`source_type: "abstract"`）。数据源级画布在「本数据源尚无
 * 编译 schema」时会回落读到它 —— 此时保存**绝不能开**：保存以 KB 级写回，正是后端
 * 本期新加的防洗行护栏（1003）要拒的那条路；前端在这里把「点了才知道」提前成
 * 「一看就不能点」，并把原因摆出来。
 *
 * 钉死三件事：
 * 1. 回落读到抽象投影 → 保存禁用 + 可见提示（Tag + 提示条）；
 * 2. 回落读到普通知识库级遗留行（无 `source_type`）→ 保存照常可用（不误伤老路径）；
 * 3. 数据源级自有 schema → 保存照常可用（没回落，与抽象无关）。
 *
 * 断言一律按 i18n key（react-i18next 被 mock 成直出键名），不锁中文文案；
 * 本仓没有 jest-dom，disabled 按原生 DOM 属性断言（同 PropertiesPanel.test.tsx 风格）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// i18n 直出键名（组件拿 t 当 effect 依赖，须稳定引用）
const { tMock, getDataMock, putDataMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
  getDataMock: vi.fn(),
  putDataMock: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

// 画布页现在带「返回」按钮：`useNavigate` 在 jsdom 里没有 Router 会抛，这里直接桩掉
const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));
vi.mock('react-router-dom', () => ({ useNavigate: () => navigateMock }));

// `readSchema` 是 `getData(request.get(url, {params}))` —— mock 直接按调用序回
// payload（等价于真 getData 解完 `{success,data}` 信封后的返回值），不去模拟 axios 细节。
vi.mock('@/api/request', () => ({
  getData: getDataMock,
  putData: putDataMock,
  request: { get: vi.fn() },
}));

import { OntologyCanvasPage } from './OntologyCanvasPage.js';

// ---------- 夹具 ----------

/** 抽象投影：`_abstract_projection` 的对外形状（source_type + 非空实体）。 */
const ABSTRACT_PROJECTION = {
  schema_version: 79,
  source_type: 'abstract',
  entity_types: [{ name: 'E1', display_name: 'E1', attributes: [] }],
  relation_types: [],
};

/** 普通知识库级遗留行：无 `source_type`（改造前的手工行）。 */
const LEGACY_KB_LEVEL = {
  schema_version: 15,
  entity_types: [{ name: 'L1', display_name: 'L1', attributes: [] }],
  relation_types: [],
};

/** 数据源级自有 schema：正常路径，不该被回落逻辑碰到。 */
const DS_LEVEL = {
  schema_version: 5,
  source_type: 'manual',
  entity_types: [{ name: 'D1', display_name: 'D1', attributes: [] }],
  relation_types: [],
};

const renderPage = () => render(<OntologyCanvasPage kbId="kb_1" dsId="ds_1" />);
const findSave = () => screen.findByRole('button', { name: /common.save/ });

beforeEach(() => {
  getDataMock.mockReset();
  putDataMock.mockReset();
});

describe('OntologyCanvasPage 防洗行护栏（抽象投影可读不可写）', () => {
  it('回落读到抽象投影：保存禁用，Tag 与提示条说明原因', async () => {
    getDataMock.mockResolvedValueOnce(null); // 数据源级这份还不存在
    getDataMock.mockResolvedValueOnce(ABSTRACT_PROJECTION); // 回落读到投影
    renderPage();

    const save = await findSave();
    expect((save as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('ontologyCanvas.abstractManaged')).toBeTruthy();
    expect(screen.getByText('ontologyCanvas.abstractManagedNotice')).toBeTruthy();
  });

  it('回落读到普通知识库级行：保存照常可用（不误伤老路径）', async () => {
    getDataMock.mockResolvedValueOnce(null);
    getDataMock.mockResolvedValueOnce(LEGACY_KB_LEVEL);
    renderPage();

    const save = await findSave();
    expect((save as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText('ontologyCanvas.abstractManagedNotice')).toBeNull();
  });

  it('数据源级自有 schema：保存照常可用（没回落，与抽象无关）', async () => {
    getDataMock.mockResolvedValueOnce(DS_LEVEL);
    renderPage();

    const save = await findSave();
    expect((save as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText('ontologyCanvas.abstractManagedNotice')).toBeNull();
  });
});


describe('画布页返回', () => {
  it('点返回回到该知识库的设置页', async () => {
    getDataMock.mockResolvedValue({ ...LEGACY_KB_LEVEL });
    renderPage();

    const back = await screen.findByRole('button', { name: /common\.back/ });
    fireEvent.click(back);

    expect(navigateMock).toHaveBeenCalledWith('/domain-knowledge/kb_1/detail');
  });
});
