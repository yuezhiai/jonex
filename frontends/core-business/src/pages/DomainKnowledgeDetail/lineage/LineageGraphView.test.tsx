/**
 * 血缘画布组件级回归（jsdom + RTL）。
 *
 * jsdom 下 React Flow 能正常挂载（`src/test/setup.ts` 已 stub ResizeObserver），
 * 所以这里直接渲染真组件，钉死可观察行为、不锁布局与内部实现：
 * - 锚点初值 = 候选第一项，请求参数（table/direction/hops）带对；
 * - 节点/边落地（实体名、边标签 = column_map 长度）；
 * - 节点点击 = 换锚点（重查）；
 * - 方向切换触发重查；
 * - `unknown` / `truncated` / 无边的表 / 无候选表 各有显式文案
 *   （`unknown` 不许画成「没有上下游」，无边不许画成空画布）；无候选表还要
 *   区分「全图成功返回空」（真空态）与「全图请求失败」（错误态 + 重试），
 *   后者不许伪装成「该源还没有表」。

 *
 * 断言按 i18n key（`react-i18next` 被 mock 成直出键名），不锁中文文案。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const { tMock, getDataAssetsMock, getLineagePathsMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
  getDataAssetsMock: vi.fn(),
  getLineagePathsMock: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/ontologyDataAssets', () => ({
  getDataAssets: getDataAssetsMock,
}));

vi.mock('@/api/ontologyLineagePaths', () => ({
  getLineagePaths: getLineagePathsMock,
}));

import LineageGraphView from './LineageGraphView';

/** 默认小图：dbo.a → dbo.b（1 条列映射）。 */
function graph(overrides: Record<string, unknown> = {}) {
  return {
    nodes: [
      { table: 'dbo.a', entity_name: '甲', attributes: [{}, {}] },
      { table: 'dbo.b', entity_name: '乙', attributes: [] },
    ],
    edges: [
      {
        from_table: 'dbo.a',
        to_table: 'dbo.b',
        column_map: [{ from_column: 'x', to_column: 'y' }],
        origin: 'manual',
      },
    ],
    unknown: false,
    truncated: false,
    ...overrides,
  };
}

/** 默认路径响应：dbo.a → dbo.b 一跳 join，命中目标。 */
function pathsResult(overrides: Record<string, unknown> = {}) {
  return {
    anchor: 'dbo.a',
    target: 'dbo.b',
    unknown: false,
    explored: 3,
    truncated: false,
    paths: [
      {
        score: 1.5,
        transfers: 1,
        hits_target: true,
        steps: [
          {
            type: 'join',
            from_table: 'dbo.a',
            from_column: 'x',
            to_table: 'dbo.b',
            to_column: 'y',
            origin: 'declared',
            reversed: false,
          },
        ],
      },
    ],
    ...overrides,
  };
}

/** 手动控制 resolve 时机的 Promise：用于钉住在途响应竞态（谁先返回、谁迟到）。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/**
 * `getDataAssets` 现在有两类调用：
 * - **锚点子图**：带 `table`；
 * - **整源全图**（锚点候选）：不带 `table`，只在挂载 / 切源时发一次。
 * 计数断言必须按类过滤，否则全图请求会把「画布重查了几次」算多。
 */
function anchoredCalls() {
  return getDataAssetsMock.mock.calls.filter((call) => call[2]?.table !== undefined);
}

function fullGraphCalls() {
  return getDataAssetsMock.mock.calls.filter((call) => call[2]?.table === undefined);
}

/**
 * 打开「目标表」下拉并选中一张表（下拉是画布控件的第 3 个 combobox）。
 * 选项按 `.ant-select-item-option` 定位于**下拉**内：画布节点也带 `title=表名`，
 * 不能直接用 `findByTitle`。
 */
async function pickTarget(table: string) {
  fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
  const option = await waitFor(() => {
    const found = [
      ...document.querySelectorAll<HTMLElement>('.ant-select-item-option'),
    ].find((el) => el.getAttribute('title') === table);
    expect(found).toBeTruthy();
    return found as HTMLElement;
  });
  fireEvent.click(option);
}

/**
 * 当前**可见**下拉里的选项。antd 关闭下拉后仍保留 DOM（加
 * `ant-select-dropdown-hidden` / 离场动画类），而多个 Select 的下拉各自独立；
 * 同一用例里先开锚点下拉、再开目标下拉时，必须按可见性过滤，否则会命中
 * 上一张下拉里同名的旧选项。
 */
function visibleOptions(): HTMLElement[] {
  const dropdowns = [
    ...document.querySelectorAll<HTMLElement>('.ant-select-dropdown'),
  ].filter(
    (el) =>
      !el.classList.contains('ant-select-dropdown-hidden') &&
      !el.className.includes('-leave'),
  );
  return dropdowns.flatMap((el) => [
    ...el.querySelectorAll<HTMLElement>('.ant-select-item-option'),
  ]);
}

/** 在指定 combobox 的下拉里选一张表（按可见下拉过滤，供同用例连续操作多个 Select）。 */
async function pickVisibleOption(comboboxIndex: number, table: string) {
  // 全图未返回前组件只渲染 loading（控件区还不存在），先等 combobox 出现。
  await waitFor(() =>
    expect(screen.getAllByRole('combobox')[comboboxIndex]).toBeTruthy(),
  );
  fireEvent.mouseDown(screen.getAllByRole('combobox')[comboboxIndex]!);
  const option = await waitFor(() => {
    const found = visibleOptions().find(
      (el) => el.getAttribute('title') === table,
    );
    expect(found).toBeTruthy();
    return found as HTMLElement;
  });
  fireEvent.click(option);
}

/** 控件区按钮（文本在 button 内）。 */
function buttonByText(text: string): HTMLButtonElement {
  return screen.getByText(text).closest('button') as HTMLButtonElement;
}

beforeEach(() => {
  vi.resetAllMocks();
  getDataAssetsMock.mockResolvedValue(graph());
  getLineagePathsMock.mockResolvedValue(pathsResult());
});

afterEach(() => {
  cleanup();
});

describe('LineageGraphView', () => {
  it('锚点初值 = 候选第一项；节点/边标签落地；点节点换锚点重查', async () => {
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);

    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.a',
        direction: 'both',
        hops: 1,
      }),
    );
    expect(await screen.findByText('甲')).toBeTruthy();
    expect(screen.getByText('乙')).toBeTruthy();
    // ⚠️ 边标签（column_map 长度）不在这里断言：jsdom 里 React Flow 拿不到节点
    // 实测尺寸（ResizeObserver 是 stub），边可能不进入渲染；标签逻辑由
    // `lineageGraphModel.test.ts` 的纯函数用例覆盖。

    // 点节点「乙」→ 设为新锚点
    fireEvent.click(screen.getByText('乙'));
    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.b',
        direction: 'both',
        hops: 1,
      }),
    );
  });

  it('revision 变化（同一表对新增第二条列级边，tables 不变）→ 重查', async () => {
    const { rerender } = render(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.b']}
        revision="1:0:dbo.a|dbo.b"
      />,
    );
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));

    // tables 一字未变，只有 revision 推进 —— Task 4 的验收场景
    rerender(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.b']}
        revision="2:0:dbo.a|dbo.b"
      />,
    );

    await waitFor(() => expect(anchoredCalls()).toHaveLength(2));
  });

  it('父级 revision / 边集刷新只重查锚点子图，不重取全图（全图每次挂载仅 1 次）', async () => {
    const { rerender } = render(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.b']}
        revision="1:0:dbo.a|dbo.b"
      />,
    );
    await waitFor(() => expect(fullGraphCalls()).toHaveLength(1));
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));

    // 父级 reload：边集换了（tables 内容与 revision 同时变化）
    rerender(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.b', 'dbo.c']}
        revision="2:1:dbo.a|dbo.b|dbo.c"
      />,
    );
    await waitFor(() => expect(anchoredCalls()).toHaveLength(2));

    // revision 再推进（同一表对新增第二条列级边，tables 一字未变）
    rerender(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.b', 'dbo.c']}
        revision="3:2:dbo.a|dbo.b|dbo.c"
      />,
    );
    await waitFor(() => expect(anchoredCalls()).toHaveLength(3));

    // 全图候选只在挂载 / 切源时取一次：父级 reload 不得重发（全图与画布用途不同）
    expect(fullGraphCalls()).toHaveLength(1);
  });

  it('方向切换 → 以新方向重查（上游/下游/双向）', async () => {
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));

    fireEvent.click(screen.getByText('domainKnowledge.lineage.graph.upstream'));

    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.a',
        direction: 'upstream',
        hops: 1,
      }),
    );
  });

  it('unknown=true → 显式「不在投影里」文案，不画空画布', async () => {
    getDataAssetsMock.mockResolvedValue(
      graph({ nodes: [], edges: [], unknown: true }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a']} />);

    expect(
      await screen.findByText(/domainKnowledge\.lineage\.graph\.unknown/),
    ).toBeTruthy();
    expect(document.querySelector('.react-flow')).toBeNull();
  });

  it('表在投影里但没有边 → 锚点单节点照画 + 「暂无上下游」提示', async () => {
    getDataAssetsMock.mockResolvedValue(
      graph({ nodes: [{ table: 'dbo.a', entity_name: '甲', attributes: [] }], edges: [] }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a']} />);

    expect(await screen.findByText('domainKnowledge.lineage.graph.noEdges')).toBeTruthy();
    expect(screen.getByText('甲')).toBeTruthy();
    expect(document.querySelector('.react-flow')).toBeTruthy();
  });

  it('truncated=true → 可见「已达上限，图已截断」告警', async () => {
    getDataAssetsMock.mockResolvedValue(graph({ truncated: true }));
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);

    expect(await screen.findByText('domainKnowledge.lineage.graph.truncated')).toBeTruthy();
  });

  it('无血缘边但全图有节点 → 锚点候选来自全图、自动选中第一张表并请求其子图（不出现空态）', async () => {
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.resolve(
              graph({
                nodes: [
                  { table: 'dbo.x', entity_name: '甲', attributes: [] },
                  { table: 'dbo.y', entity_name: '乙', attributes: [] },
                ],
                edges: [],
              }),
            )
          : Promise.resolve(graph()),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={[]} />);

    // 排序后的第一张表（props 为空）成为默认锚点，并请求它的子图
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));
    expect(anchoredCalls()[0]![2]).toEqual({
      table: 'dbo.x',
      direction: 'both',
      hops: 1,
    });
    // 旧的「先有血缘边」空态不再出现
    expect(screen.queryByText('domainKnowledge.lineage.graph.noTables')).toBeNull();

    // 锚点下拉里有全图的全部节点
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]!);
    await waitFor(() => {
      const options = [
        ...document.querySelectorAll<HTMLElement>('.ant-select-item-option'),
      ].map((el) => el.getAttribute('title'));
      expect(options.sort()).toEqual(['dbo.x', 'dbo.y']);
    });
  });

  it('全图请求失败 → 静默回退到血缘边表清单（不新增错误 UI，画布照常加载）', async () => {
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.reject(new Error('全图查询失败'))
          : Promise.resolve(graph()),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);

    await waitFor(() => expect(fullGraphCalls()).toHaveLength(1));
    // 画布仍按 props 锚点加载
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));
    expect(anchoredCalls()[0]![2]).toEqual({
      table: 'dbo.a',
      direction: 'both',
      hops: 1,
    });
    expect(await screen.findByText('甲')).toBeTruthy();
    // 全图失败不冒错误 UI（错误 message 不出现在任何位置）
    expect(screen.queryByText('全图查询失败')).toBeNull();

    // 候选回退到 props（全图里没有任何表）
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]!);
    await waitFor(() => {
      const options = [
        ...document.querySelectorAll<HTMLElement>('.ant-select-item-option'),
      ].map((el) => el.getAttribute('title'));
      expect(options.sort()).toEqual(['dbo.a', 'dbo.b']);
    });
  });

  it('全图失败且无血缘边表 → 显式错误态 + 重试；重试成功后候选/画布恢复', async () => {
    let fullGraphAttempts = 0;
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) => {
        if (params.table === undefined) {
          fullGraphAttempts += 1;
          return fullGraphAttempts === 1
            ? Promise.reject(new Error('全图查询失败'))
            : Promise.resolve(graph());
        }
        return Promise.resolve(graph());
      },
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={[]} />);

    // 失败 + 无 props 候选 ⇒ 显式错误态（后端 message 原样透出），不得伪装成
    // 「该源还没有可用的数据资产表」的假空态。
    expect(await screen.findByText('全图查询失败')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.graph.noTables')).toBeNull();
    const retry = buttonByText('common.retry');

    fireEvent.click(retry);

    // 重试 = 第二次全图请求；成功后候选来自全图、自动选中第一张并渲染画布。
    await waitFor(() => expect(fullGraphCalls()).toHaveLength(2));
    await waitFor(() => expect(anchoredCalls()).toHaveLength(1));
    expect(anchoredCalls()[0]![2]).toEqual({
      table: 'dbo.a',
      direction: 'both',
      hops: 1,
    });
    expect(await screen.findByText('甲')).toBeTruthy();
    expect(screen.queryByText('全图查询失败')).toBeNull();
    expect(screen.queryByText('common.retry')).toBeNull();
  });

  it('全图失败且无血缘边表、错误没有 message → 通用「加载表清单失败」文案', async () => {
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.reject(new Error())
          : Promise.resolve(graph()),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={[]} />);

    expect(
      await screen.findByText('domainKnowledge.lineage.graph.fullGraphError'),
    ).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.graph.noTables')).toBeNull();
  });

  it('全图失败时点节点选的锚点不在候选里 → 父级换边集后仍保留，不回落重置', async () => {
    // 全图失败 ⇒ 候选只有 props（dbo.a）。点画布节点 dbo.b 换锚点：dbo.b 只
    // 存在于当前画布子图里、不在候选。父级随后 reload（tables/revision 变化）。
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.reject(new Error('全图查询失败'))
          : Promise.resolve(graph()),
    );
    const { rerender } = render(
      <LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a']} revision="1:0:dbo.a" />,
    );
    await waitFor(() => expect(fullGraphCalls()).toHaveLength(1));
    expect(await screen.findByText('乙')).toBeTruthy();

    // 点节点「乙」(dbo.b) → 锚点切到 dbo.b
    fireEvent.click(screen.getByText('乙'));
    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.b',
        direction: 'both',
        hops: 1,
      }),
    );

    // 父级 reload：边集变化 ⇒ 锚点默认回落 effect 重跑
    rerender(
      <LineageGraphView
        kbId="kb_1"
        dsId="ds_a"
        tables={['dbo.a', 'dbo.c']}
        revision="2:1:dbo.a|dbo.c"
      />,
    );

    // 锚点仍是用户点选的 dbo.b，不被重置回第一个候选 dbo.a：
    // 初始 dbo.a + 点击 dbo.b + reload dbo.b = 3 次；若回落重置会多出一次 dbo.a。
    await waitFor(() => expect(anchoredCalls()).toHaveLength(3));
    await act(async () => {}); // 冲掉挂起的 effect/微任务，避免漏计回落请求
    expect(anchoredCalls().map((call) => call[2]?.table)).toEqual([
      'dbo.a',
      'dbo.b',
      'dbo.b',
    ]);
  });

  it('全图 truncated → 锚点选择旁显示「表清单已截断」提示', async () => {
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.resolve(graph({ truncated: true }))
          : Promise.resolve(graph()),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);

    expect(
      await screen.findByText('domainKnowledge.lineage.graph.anchorTruncated'),
    ).toBeTruthy();
  });

  it('全图与血缘边都为空 → 新的「没有可用数据资产表」空态，不发锚点子图请求', async () => {
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.resolve(graph({ nodes: [], edges: [] }))
          : Promise.resolve(graph()),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={[]} />);

    expect(await screen.findByText('domainKnowledge.lineage.graph.noTables')).toBeTruthy();
    // 成功返回空 ⇒ 真·空态：不提供重试（重试也只会再得到空）
    expect(screen.queryByText('common.retry')).toBeNull();
    expect(anchoredCalls()).toHaveLength(0);
    expect(fullGraphCalls()).toHaveLength(1);
  });

  it('请求失败 → 后端 message 原样透出；点重试发起新请求并恢复画布', async () => {
    let anchorAttempts = 0;
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) => {
        if (params.table === undefined) return Promise.resolve(graph());
        anchorAttempts += 1;
        return anchorAttempts === 1
          ? Promise.reject(new Error('网关拒绝：direction 非法'))
          : Promise.resolve(graph());
      },
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a']} />);

    expect(await screen.findByText('网关拒绝：direction 非法')).toBeTruthy();
    expect(anchoredCalls()).toHaveLength(1);

    fireEvent.click(screen.getByText('common.retry'));

    // 重试是一次新请求（本次 mock 已回落为成功），错误态被结果替换
    await waitFor(() => expect(anchoredCalls()).toHaveLength(2));
    expect(await screen.findByText('甲')).toBeTruthy();
    expect(screen.queryByText('网关拒绝：direction 非法')).toBeNull();
  });
});

describe('LineageGraphView——路径高亮（P2b）', () => {
  it('目标表候选排除锚点；高亮请求参数正确；面板列清单；清除后面板消失', async () => {
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();

    // 未选目标 ⇒ 高亮按钮禁用
    const highlightButton = buttonByText('domainKnowledge.lineage.paths.highlight');
    expect(highlightButton.disabled).toBe(true);

    // 下拉只列「画布节点 - 锚点」：dbo.a 是锚点，不出现在选项里
    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    const option = await waitFor(() => {
      const options = document.querySelectorAll<HTMLElement>('.ant-select-item-option');
      expect(options).toHaveLength(1);
      expect(options[0]!.getAttribute('title')).toBe('dbo.b');
      return options[0]!;
    });
    fireEvent.click(option);

    // 选完目标后重查按钮节点（disabled 已随状态更新）
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));

    await waitFor(() =>
      expect(getLineagePathsMock).toHaveBeenCalledWith('kb_1', 'ds_a', {
        anchor: 'dbo.a',
        target: 'dbo.b',
        maxHops: 1,
        maxPaths: 5,
      }),
    );

    // 面板：标题 / 路径序号 / score(2 位) / transfers / 命中目标 / 步骤
    expect(await screen.findByText('domainKnowledge.lineage.paths.panelTitle')).toBeTruthy();
    expect(
      screen.getByText((content) => content.includes('"score":"1.50"')),
    ).toBeTruthy();
    expect(
      screen.getByText((content) => content.includes('paths.transfers({"count":1})')),
    ).toBeTruthy();
    expect(screen.getByText('domainKnowledge.lineage.paths.hitsTarget')).toBeTruthy();
    expect(screen.getByText('dbo.a.x → dbo.b.y')).toBeTruthy();
    expect(screen.getByText('domainKnowledge.lineage.paths.typeJoin')).toBeTruthy();
    // originDeclared 同时出现在图例与面板 step 标签上
    expect(
      screen.getAllByText('domainKnowledge.lineage.originDeclared').length,
    ).toBeGreaterThanOrEqual(2);

    // 清除 ⇒ 面板隐藏（边恢复原始样式的输入由 `applyPathHighlights` 用例锁定）
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.clear'));
    await waitFor(() =>
      expect(screen.queryByText('domainKnowledge.lineage.paths.panelTitle')).toBeNull(),
    );
  });

  it('路径步骤不在画布子图内 → 面板给出 missed 说明，清单仍列出该步骤', async () => {
    getLineagePathsMock.mockResolvedValue(
      pathsResult({
        paths: [
          {
            score: 2,
            transfers: 0,
            hits_target: true,
            steps: [
              {
                type: 'join',
                from_table: 'dbo.a',
                from_column: 'id',
                to_table: 'dbo.ghost',
                to_column: 'id',
                origin: 'derived',
                reversed: false,
              },
            ],
          },
        ],
      }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));

    expect(
      await screen.findByText('domainKnowledge.lineage.paths.missedEdges'),
    ).toBeTruthy();
    expect(screen.getByText('dbo.a.id → dbo.ghost.id')).toBeTruthy();
  });

  it('reversed 步骤 → 箭头取遍历方向 ←，端点仍是数据流端点、不互换', async () => {
    // 验收场景：anchor=dbo.b 逆数据流找 dbo.a（数据流本身是 dbo.a → dbo.b）。
    // 面板必须显示 `dbo.a.x ← dbo.b.y`，而不是把端点跟着遍历方向换过来。
    getLineagePathsMock.mockResolvedValue(
      pathsResult({
        anchor: 'dbo.b',
        target: 'dbo.a',
        paths: [
          {
            score: 1,
            transfers: 1,
            hits_target: true,
            steps: [
              {
                type: 'join',
                from_table: 'dbo.a',
                from_column: 'x',
                to_table: 'dbo.b',
                to_column: 'y',
                origin: 'declared',
                reversed: true,
              },
            ],
          },
        ],
      }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);

    // 点节点「乙」→ 锚点切到 dbo.b（遍历起点），目标选 dbo.a
    expect(await screen.findByText('乙')).toBeTruthy();
    fireEvent.click(screen.getByText('乙'));
    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.b',
        direction: 'both',
        hops: 1,
      }),
    );
    await pickTarget('dbo.a');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));

    expect(await screen.findByText('dbo.a.x ← dbo.b.y')).toBeTruthy();
    expect(screen.queryByText('dbo.b.y ← dbo.a.x')).toBeNull();
  });

  it('unknown=true → 显式「没有可用编译 schema」，不渲染成「没有找到路径」', async () => {
    getLineagePathsMock.mockResolvedValue(
      pathsResult({ unknown: true, explored: 0, paths: [] }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));

    expect(await screen.findByText('domainKnowledge.lineage.paths.unknown')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.paths.empty')).toBeNull();
    // unknown 仍要显示求值元信息（explored=0；truncated=false 不出 Tag）——
    // 「该源没有编译 schema」与「这次求值扫了多少」是两个维度。
    expect(
      screen.getByText('domainKnowledge.lineage.paths.explored({"count":0})'),
    ).toBeTruthy();
    expect(
      screen.queryByText('domainKnowledge.lineage.paths.truncated({"max":5})'),
    ).toBeNull();
  });

  it('paths=[] 且非 unknown → 「没有找到路径」；truncated 有警示文案', async () => {
    getLineagePathsMock.mockResolvedValue(
      pathsResult({ paths: [], explored: 12, truncated: true }),
    );
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));

    expect(await screen.findByText('domainKnowledge.lineage.paths.empty')).toBeTruthy();
    expect(
      screen.getByText('domainKnowledge.lineage.paths.explored({"count":12})'),
    ).toBeTruthy();
    expect(
      screen.getByText('domainKnowledge.lineage.paths.truncated({"max":5})'),
    ).toBeTruthy();
  });

  it('路径请求在途 → loading 文案；失败 → 后端 message 原样透出', async () => {
    getLineagePathsMock.mockReturnValueOnce(new Promise(() => {}));
    const { unmount } = render(
      <LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />,
    );
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    expect(await screen.findByText('domainKnowledge.lineage.paths.loading')).toBeTruthy();
    unmount();

    // 失败态：后端 message 原样透出（不吞成通用文案）
    getLineagePathsMock.mockRejectedValueOnce(new Error('网关拒绝：target 非法'));
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    expect(await screen.findByText('网关拒绝：target 非法')).toBeTruthy();
    expect(screen.getByText('domainKnowledge.lineage.paths.error')).toBeTruthy();
  });

  it('换方向（画布重查）→ 旧高亮/面板自动清除', async () => {
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    expect(await screen.findByText('domainKnowledge.lineage.paths.panelTitle')).toBeTruthy();

    fireEvent.click(screen.getByText('domainKnowledge.lineage.graph.upstream'));

    // 等重查真的完成（画布重新出现）再断言：loading 期间面板本来就不渲染，
    // 只在那一刻断言会漏掉「重查后高亮仍在」的缺陷。
    await waitFor(() => expect(anchoredCalls()).toHaveLength(2));
    expect(await screen.findByText('甲')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.paths.panelTitle')).toBeNull();
  });

  it('在途路径响应过期（先 dbo.b 后 dbo.c，dbo.b 迟到）→ 丢弃旧响应，面板保持新结果', async () => {
    getDataAssetsMock.mockResolvedValue(
      graph({
        nodes: [
          { table: 'dbo.a', entity_name: '甲', attributes: [] },
          { table: 'dbo.b', entity_name: '乙', attributes: [] },
          { table: 'dbo.c', entity_name: '丙', attributes: [] },
        ],
        edges: [
          {
            from_table: 'dbo.a',
            to_table: 'dbo.b',
            column_map: [{ from_column: 'x', to_column: 'y' }],
            origin: 'manual',
          },
          {
            from_table: 'dbo.a',
            to_table: 'dbo.c',
            column_map: [{ from_column: 'x', to_column: 'z' }],
            origin: 'manual',
          },
        ],
      }),
    );
    const first = deferred<ReturnType<typeof pathsResult>>();
    const second = deferred<ReturnType<typeof pathsResult>>();
    getLineagePathsMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b', 'dbo.c']} />);
    expect(await screen.findByText('甲')).toBeTruthy();

    // 第一次请求（目标 dbo.b），保持挂起
    await pickTarget('dbo.b');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    await waitFor(() => expect(getLineagePathsMock).toHaveBeenCalledTimes(1));

    // 换目标 dbo.c（旧请求作废）→ 第二次请求，同样挂起
    await pickTarget('dbo.c');
    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    await waitFor(() => expect(getLineagePathsMock).toHaveBeenCalledTimes(2));

    // 第二次先返回：面板显示 explored=22 与 dbo.c 的步骤
    await act(async () => {
      second.resolve(
        pathsResult({
          target: 'dbo.c',
          explored: 22,
          paths: [
            {
              score: 2,
              transfers: 1,
              hits_target: true,
              steps: [
                {
                  type: 'join',
                  from_table: 'dbo.a',
                  from_column: 'x',
                  to_table: 'dbo.c',
                  to_column: 'z',
                  origin: 'declared',
                  reversed: false,
                },
              ],
            },
          ],
        }),
      );
    });
    expect(
      await screen.findByText('domainKnowledge.lineage.paths.explored({"count":22})'),
    ).toBeTruthy();
    expect(screen.getByText('dbo.a.x → dbo.c.z')).toBeTruthy();

    // 第一次迟到：必须被 `pathsReqRef` 丢弃，不得覆盖面板/高亮
    await act(async () => {
      first.resolve(pathsResult({ target: 'dbo.b', explored: 11 }));
    });
    expect(
      screen.getByText('domainKnowledge.lineage.paths.explored({"count":22})'),
    ).toBeTruthy();
    expect(
      screen.queryByText('domainKnowledge.lineage.paths.explored({"count":11})'),
    ).toBeNull();
    expect(screen.getByText('dbo.a.x → dbo.c.z')).toBeTruthy();
    expect(screen.queryByText('dbo.a.x → dbo.b.y')).toBeNull();
  });

  it('join-only 源（无血缘边）→ 目标候选仍含全图表；可高亮 join 路径并提示步骤不在画布上', async () => {
    // 验收现场：jinpin 0 条血缘边。全图里有 dbo.jobs / dbo.job_forward，但锚点
    // dbo.jobs 的子图只有锚点单节点（无边）—— 旧实现只取画布节点 ⇒ 目标下拉为空。
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) => {
        if (params.table === undefined) {
          return Promise.resolve(
            graph({
              nodes: [
                { table: 'dbo.jobs', entity_name: '职位', attributes: [] },
                { table: 'dbo.job_forward', entity_name: '转发表', attributes: [] },
              ],
              edges: [],
            }),
          );
        }
        return Promise.resolve(
          graph({
            nodes: [
              {
                table: params.table,
                entity_name: params.table === 'dbo.jobs' ? '职位' : '转发表',
                attributes: [],
              },
            ],
            edges: [],
          }),
        );
      },
    );
    getLineagePathsMock.mockResolvedValue(
      pathsResult({
        anchor: 'dbo.jobs',
        target: 'dbo.job_forward',
        paths: [
          {
            score: 2,
            transfers: 1,
            hits_target: true,
            steps: [
              {
                type: 'join',
                from_table: 'dbo.jobs',
                from_column: 'id',
                to_table: 'dbo.job_forward',
                to_column: 'job_id',
                origin: 'declared',
                reversed: false,
              },
            ],
          },
        ],
      }),
    );

    // tables prop 为空（没有血缘边）⇒ 锚点从全图候选中手动选 dbo.jobs。
    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={[]} />);
    await pickVisibleOption(0, 'dbo.jobs');
    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.jobs',
        direction: 'both',
        hops: 1,
      }),
    );
    // 锚点子图只有 dbo.jobs：表在投影里、没有任何边。
    expect(await screen.findByText('职位')).toBeTruthy();
    expect(screen.getByText('domainKnowledge.lineage.graph.noEdges')).toBeTruthy();

    // 目标下拉必须列全图里的 dbo.job_forward（画布节点 − 锚点 = 空）。
    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    await waitFor(() => {
      expect(visibleOptions().map((el) => el.getAttribute('title'))).toEqual([
        'dbo.job_forward',
      ]);
    });
    fireEvent.click(visibleOptions()[0]!);

    fireEvent.click(buttonByText('domainKnowledge.lineage.paths.highlight'));
    await waitFor(() =>
      expect(getLineagePathsMock).toHaveBeenCalledWith('kb_1', 'ds_a', {
        anchor: 'dbo.jobs',
        target: 'dbo.job_forward',
        maxHops: 1,
        maxPaths: 5,
      }),
    );

    // join 步骤不在画布边里（画布无边）→ 清单仍完整列出，并标注「不在当前画布」。
    expect(
      await screen.findByText('dbo.jobs.id → dbo.job_forward.job_id'),
    ).toBeTruthy();
    expect(
      screen.getByText('domainKnowledge.lineage.paths.missedEdges'),
    ).toBeTruthy();
  });

  it('全图不可用（回退画布节点）时换锚点后目标表不在新子图 → target 自动清空', async () => {
    // dbo.a 的子图含 dbo.b / dbo.c；dbo.c 的子图只剩 dbo.d —— 换锚点后 dbo.b 悬空。
    // 全图请求失败 ⇒ 目标候选回退到当前画布节点，旧的自愈行为仍然成立。
    getDataAssetsMock.mockImplementation(
      (_kbId: string, _dsId: string, params: { table?: string } = {}) =>
        params.table === undefined
          ? Promise.reject(new Error('全图查询失败'))
          : Promise.resolve(
              params.table === 'dbo.c'
                ? graph({
                    nodes: [
                      { table: 'dbo.c', entity_name: '丙', attributes: [] },
                      { table: 'dbo.d', entity_name: '丁', attributes: [] },
                    ],
                    edges: [
                      {
                        from_table: 'dbo.c',
                        to_table: 'dbo.d',
                        column_map: [{ from_column: 'x', to_column: 'y' }],
                        origin: 'manual',
                      },
                    ],
                  })
                : graph({
                    nodes: [
                      { table: 'dbo.a', entity_name: '甲', attributes: [] },
                      { table: 'dbo.b', entity_name: '乙', attributes: [] },
                      { table: 'dbo.c', entity_name: '丙', attributes: [] },
                    ],
                    edges: [
                      {
                        from_table: 'dbo.a',
                        to_table: 'dbo.b',
                        column_map: [{ from_column: 'x', to_column: 'y' }],
                        origin: 'manual',
                      },
                      {
                        from_table: 'dbo.a',
                        to_table: 'dbo.c',
                        column_map: [{ from_column: 'x', to_column: 'z' }],
                        origin: 'manual',
                      },
                    ],
                  }),
            ),
    );

    render(<LineageGraphView kbId="kb_1" dsId="ds_a" tables={['dbo.a', 'dbo.b']} />);
    expect(await screen.findByText('甲')).toBeTruthy();

    await pickTarget('dbo.b');
    await waitFor(() =>
      expect(buttonByText('domainKnowledge.lineage.paths.highlight').disabled).toBe(false),
    );

    // 点节点「丙」→ 锚点变为 dbo.c，新子图里没有 dbo.b
    fireEvent.click(screen.getByText('丙'));
    await waitFor(() =>
      expect(getDataAssetsMock).toHaveBeenLastCalledWith('kb_1', 'ds_a', {
        table: 'dbo.c',
        direction: 'both',
        hops: 1,
      }),
    );
    expect(await screen.findByText('丁')).toBeTruthy();

    // target 被自动清空 → 高亮按钮重新禁用（若 target 残留在悬空值上则仍可用）
    await waitFor(() =>
      expect(buttonByText('domainKnowledge.lineage.paths.highlight').disabled).toBe(true),
    );
    expect(screen.getByText('domainKnowledge.lineage.paths.targetPlaceholder')).toBeTruthy();
  });
});
