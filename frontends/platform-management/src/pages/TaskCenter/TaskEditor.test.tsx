// @vitest-environment jsdom
/**
 * 「编辑任务」页：编排的加载、增删与保存。
 *
 * 页面完全由后端接口驱动，所以测试把接口层整体替身掉，断言的是**出参的形状**：
 * 画布是单链、保存时 `depends_on` / `next_steps` 成对、开始入参改名后绑定跟着改。
 * 这些是后端校验器（N2 / N4 / N8b）真正会拒的地方，界面再好看，形状错了也存不进去。
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { TaskCatalogStep, TaskDefinitionDetail, VersionDetail } from '../../api/taskCenterDefinitions';

const getDefinition = vi.fn();
const getDefinitionVersion = vi.fn();
const listCatalogSteps = vi.fn();
const saveOrchestration = vi.fn();

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh-CN' } }),
}));

vi.mock('@jonex/shared-lib', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    PageHeader: ({ title, extra }: { title: ReactNode; extra?: ReactNode }) => (
      <div>
        <h1>{title}</h1>
        {extra}
      </div>
    ),
    PageContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    usePermission: () => ({ hasPerm: () => true }),
  };
});

vi.mock('react-router-dom', () => ({
  useParams: () => ({ definitionId: 'def-1' }),
  useLocation: () => ({ state: null }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../../api/taskCenterDefinitions', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    getDefinition: (...args: unknown[]) => getDefinition(...args),
    getDefinitionVersion: (...args: unknown[]) => getDefinitionVersion(...args),
    listCatalogSteps: (...args: unknown[]) => listCatalogSteps(...args),
    saveOrchestration: (...args: unknown[]) => saveOrchestration(...args),
  };
});

import TaskEditor from './TaskEditor';

const CATALOG: TaskCatalogStep[] = [
  {
    id: 'step-parse',
    step_code: 'parse',
    name: '文件解析',
    runner_code: 'knowledge_base',
    timeout_seconds: 25200,
    input_schema: {
      document: { type: 'file', required: true },
      knowledge_id: { type: 'string', required: true },
    },
    output_schema: {
      chunks: { type: 'object', abstract: true },
      document_id: { type: 'string', required: true },
    },
  },
  {
    id: 'step-compile',
    step_code: 'compile',
    name: '知识编译',
    runner_code: 'knowledge_base',
    timeout_seconds: 25200,
    input_schema: {
      chunks: { type: 'object', abstract: true, required: true },
      knowledge_id: { type: 'string', required: true },
      document_id: { type: 'string', required: true },
    },
    output_schema: {
      ontology: { type: 'object', abstract: true },
      wiki: { type: 'object', abstract: true },
    },
  },
];

const DETAIL: TaskDefinitionDetail = {
  definition: {
    id: 'def-1',
    scope: 'space',
    space_id: 'space-1',
    name: '知识库文档解析与编译',
    status: 'published',
    current_version_no: 3,
    execution_count: 0,
    last_execution_result: null,
  },
  space_id: 'space-1',
  versions: [{ version_no: 3, status: 'published' }],
  draft_version_no: null,
};

const VERSION: VersionDetail = {
  id: 'version-3',
  definition_id: 'def-1',
  version_no: 3,
  status: 'published',
  config: {
    start: {
      node_id: 'start',
      inputs: {
        document: { type: 'file', required: true },
        knowledge_id: { type: 'string', required: true },
      },
    },
    steps: [
      {
        node_id: 'parse_1',
        step_code: 'parse',
        depends_on: [],
        next_steps: ['compile_1'],
        sort: 1,
        timeout_seconds: 25200,
        max_retries: 0,
        on_retry_exhausted: 'terminate',
        bind_inputs: {
          document: '@task.inputs.document',
          knowledge_id: '@task.inputs.knowledge_id',
        },
      },
      {
        node_id: 'compile_1',
        step_code: 'compile',
        depends_on: ['parse_1'],
        next_steps: [],
        sort: 2,
        timeout_seconds: 25200,
        max_retries: 0,
        on_retry_exhausted: 'terminate',
        bind_inputs: {
          chunks: '@parse_1.outputs.chunks',
          knowledge_id: '@task.inputs.knowledge_id',
          document_id: '@parse_1.outputs.document_id',
        },
      },
    ],
    end: { node_id: 'end', outputs: {} },
  },
};

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

/** 等首屏的两次并发请求落地、画布出现 */
const renderEditor = async (): Promise<void> => {
  render(<TaskEditor />);
  await screen.findByTestId('workflow-canvas');
};

/** 当前展开的那个浮层（antd 关闭后只是加 hidden 类，旧内容仍留在 DOM 里） */
const visiblePopover = (): HTMLElement => {
  const node = document.querySelector('.ant-popover:not(.ant-popover-hidden)');
  expect(node).not.toBeNull();
  return node as HTMLElement;
};

beforeEach(() => {
  getDefinition.mockReset().mockResolvedValue(DETAIL);
  getDefinitionVersion.mockReset().mockResolvedValue(VERSION);
  listCatalogSteps.mockReset().mockResolvedValue(CATALOG);
  saveOrchestration.mockReset().mockResolvedValue(VERSION);
});

afterEach(() => cleanup());

describe('TaskEditor 加载与画布', () => {
  it('按已发布版本的编排渲染开始 / 两个业务节点 / 结束', async () => {
    await renderEditor();
    const canvas = screen.getByTestId('workflow-canvas');
    // 步骤名走本地化：测试替身让 t 原样回 key，未命中的 step_code 回落到清单里的中文名
    expect(within(canvas).getByText('文件解析')).toBeTruthy();
    expect(within(canvas).getByText('知识编译')).toBeTruthy();
    expect(within(canvas).getAllByText('taskCenter.editor.node.start').length).toBeGreaterThan(0);
    expect(within(canvas).getAllByText('taskCenter.editor.node.end').length).toBeGreaterThan(0);
    // 两个业务节点各有一个删除入口，端点没有
    expect(within(canvas).getAllByLabelText('taskCenter.editor.action.deleteNode')).toHaveLength(2);
    expect(getDefinitionVersion).toHaveBeenCalledWith('def-1', 3);
  });

  it('有草稿时优先载入草稿版本', async () => {
    getDefinition.mockResolvedValue({ ...DETAIL, draft_version_no: 4 });
    getDefinitionVersion.mockResolvedValue({ ...VERSION, version_no: 4, status: 'draft' });
    await renderEditor();
    expect(getDefinitionVersion).toHaveBeenCalledWith('def-1', 4);
    // 载入的是草稿：不提示「还没有草稿，保存会新开一版」
    expect(screen.queryByText('taskCenter.editor.draftHint')).toBeNull();
  });

  it('点步骤卡切换右侧面板，输入段用绑定下拉而不是编辑框', async () => {
    await renderEditor();
    fireEvent.click(screen.getByText('文件解析'));

    for (const key of [
      'taskCenter.editor.section.input',
      'taskCenter.editor.section.output',
      'taskCenter.editor.section.advanced',
      'taskCenter.editor.output.readonly',
      'taskCenter.editor.field.timeout',
      'taskCenter.editor.field.maxRuns',
      'taskCenter.editor.field.retryInterval',
    ]) {
      expect(screen.getAllByText(key).length).toBeGreaterThan(0);
    }
    // 解析的 document / knowledge_id 已按同名自动绑定，下拉上显示的是来源而不是占位符
    expect(screen.queryByText('taskCenter.editor.field.upstreamPlaceholder')).toBeNull();
    // 下拉里显示的是已选来源（「流程入参 · document」），而不是占位符
    expect(screen.getByText(/· document$/)).toBeTruthy();
    // 必填入参的字段名后面带星号
    expect(screen.getByText(/^document \*$/)).toBeTruthy();
    expect(screen.getByText('chunks')).toBeTruthy();
    // 抽象对象按文档口径显示 Object，而不是裸的 object
    expect(screen.getAllByText('Object').length).toBeGreaterThan(0);
  });

  it('开始节点的入参可增删，空变量名给红字提示', async () => {
    await renderEditor();
    expect(screen.getAllByPlaceholderText('taskCenter.editor.field.varNamePlaceholder')).toHaveLength(2);

    fireEvent.click(screen.getByLabelText('taskCenter.editor.action.addInput'));
    expect(screen.getAllByPlaceholderText('taskCenter.editor.field.varNamePlaceholder')).toHaveLength(3);
    expect(screen.getByText('taskCenter.editor.field.nameRequired')).toBeTruthy();

    fireEvent.click(screen.getAllByLabelText('taskCenter.editor.action.removeInput')[2]);
    await waitFor(() => expect(screen.queryByText('taskCenter.editor.field.nameRequired')).toBeNull());
  });

  it('删掉解析节点会连带清掉编译节点', async () => {
    await renderEditor();
    const canvas = screen.getByTestId('workflow-canvas');
    const parseCard = within(canvas).getByText('文件解析').closest('[data-node-id]') as HTMLElement;
    fireEvent.click(within(parseCard).getByLabelText('taskCenter.editor.action.deleteNode'));

    await waitFor(() => expect(within(canvas).queryByText('文件解析')).toBeNull());
    expect(within(canvas).queryByText('知识编译')).toBeNull();
    expect(within(canvas).queryAllByLabelText('taskCenter.editor.action.deleteNode')).toHaveLength(0);
  });

  it('添加菜单按清单出卡：已加的置灰，缺解析时编译不可加', async () => {
    await renderEditor();
    // 两个步骤都在编排里：菜单里两张卡都是「已添加」
    fireEvent.click(screen.getByTestId('add-step-button'));
    await waitFor(() => visiblePopover());
    expect(within(visiblePopover()).getAllByText('taskCenter.editor.step.added')).toHaveLength(2);
    fireEvent.click(screen.getByTestId('add-step-button'));
    await waitFor(() => expect(document.querySelector('.ant-popover:not(.ant-popover-hidden)')).toBeNull());

    // 删掉解析（连带编译）后，编译卡改为提示「先加解析」
    const canvas = screen.getByTestId('workflow-canvas');
    const parseCard = within(canvas).getByText('文件解析').closest('[data-node-id]') as HTMLElement;
    fireEvent.click(within(parseCard).getByLabelText('taskCenter.editor.action.deleteNode'));
    fireEvent.click(screen.getByTestId('add-step-button'));
    await waitFor(() => visiblePopover());
    expect(within(visiblePopover()).getByText('taskCenter.editor.step.needParse')).toBeTruthy();
    expect(within(visiblePopover()).queryByText('taskCenter.editor.step.added')).toBeNull();
  });
});

describe('TaskEditor 保存', () => {
  const buttons = () => ({
    save: screen.getByText('common.save').closest('button') as HTMLButtonElement,
    publish: screen.getByText('taskCenter.editor.action.publish').closest('button') as HTMLButtonElement,
  });

  /** 制造一处未保存的修改：把超时时间改大（不影响其余断言） */
  const makeDirty = async (): Promise<void> => {
    fireEvent.click(screen.getByText('知识编译'));
    fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '600' } });
  };

  it('没有未保存的修改时，保存与发布都点不动', async () => {
    await renderEditor();
    // 载入的是已发布版本且一字未改：两个动作都应该是灰的
    expect(buttons().save.disabled).toBe(true);
    expect(buttons().publish.disabled).toBe(true);
    expect(buttons().save.getAttribute('title')).toBe('taskCenter.editor.action.noChanges');
  });

  it('载入的就是草稿时，没改动也能发布（把已有草稿发出去是有意义的动作）', async () => {
    getDefinition.mockResolvedValue({ ...DETAIL, draft_version_no: 4 });
    getDefinitionVersion.mockResolvedValue({ ...VERSION, version_no: 4, status: 'draft' });
    await renderEditor();
    expect(buttons().save.disabled).toBe(true);
    expect(buttons().publish.disabled).toBe(false);
  });

  it('存草稿：改过配置才能存，把画布串成单链、绑定原样下发', async () => {
    await renderEditor();
    await makeDirty();
    expect(buttons().save.disabled).toBe(false);
    fireEvent.click(screen.getByText('common.save'));

    await waitFor(() => expect(saveOrchestration).toHaveBeenCalledTimes(1));
    const [definitionId, config, status] = saveOrchestration.mock.calls[0];
    expect(definitionId).toBe('def-1');
    expect(status).toBe('draft');
    expect(config.start.inputs).toEqual({
      document: { type: 'file', required: true },
      knowledge_id: { type: 'string', required: true },
    });
    expect(config.steps.map((step: { node_id: string }) => step.node_id)).toEqual(['parse_1', 'compile_1']);
    expect(config.steps[0].depends_on).toEqual([]);
    expect(config.steps[0].next_steps).toEqual(['compile_1']);
    expect(config.steps[1].depends_on).toEqual(['parse_1']);
    expect(config.steps[1].next_steps).toEqual([]);
    expect(config.steps[1].bind_inputs).toEqual({
      chunks: '@parse_1.outputs.chunks',
      knowledge_id: '@task.inputs.knowledge_id',
      document_id: '@parse_1.outputs.document_id',
    });
    expect(config.end).toEqual({ node_id: 'end', outputs: {} });
  });

  it('发布：改过配置后同一个接口换 status，成功后重取定义', async () => {
    await renderEditor();
    await makeDirty();
    expect(buttons().publish.disabled).toBe(false);
    getDefinition.mockClear();
    fireEvent.click(screen.getByText('taskCenter.editor.action.publish'));

    await waitFor(() => expect(saveOrchestration).toHaveBeenCalledTimes(1));
    expect(saveOrchestration.mock.calls[0][2]).toBe('published');
    await waitFor(() => expect(getDefinition).toHaveBeenCalled());
  });

  it('改流程入参名，步骤里引用它的绑定跟着改', async () => {
    await renderEditor();
    // 第一个入参是 document（file）；改成 doc
    const nameInput = screen.getAllByPlaceholderText('taskCenter.editor.field.varNamePlaceholder')[0];
    fireEvent.change(nameInput, { target: { value: 'doc' } });

    fireEvent.click(screen.getByText('common.save'));
    await waitFor(() => expect(saveOrchestration).toHaveBeenCalledTimes(1));
    const config = saveOrchestration.mock.calls[0][1];
    expect(Object.keys(config.start.inputs)).toEqual(['doc', 'knowledge_id']);
    expect(config.steps[0].bind_inputs.document).toBe('@task.inputs.doc');
  });

  it('必填入参没绑来源时拦在保存前，不发请求', async () => {
    await renderEditor();
    // 删掉第一个流程入参（document）：解析的 document 绑定随之清空，必填项失去来源
    fireEvent.click(screen.getAllByLabelText('taskCenter.editor.action.removeInput')[0]);

    fireEvent.click(screen.getByText('common.save'));
    await waitFor(() => expect(screen.getByText(/taskCenter\.editor\.field\.bindRequired/)).toBeTruthy());
    expect(saveOrchestration).not.toHaveBeenCalled();
  });

  it('知识编译的输入跟随文档解析：只展示来源，不给编辑', async () => {
    await renderEditor();
    fireEvent.click(screen.getByText('知识编译'));

    // 三个入参全部展示为推导出来的来源
    expect(screen.getByText('文件解析 · parse_1.chunks')).toBeTruthy();
    expect(screen.getByText('文件解析 · parse_1.document_id')).toBeTruthy();
    expect(screen.getByText('taskCenter.editor.section.input · knowledge_id')).toBeTruthy();
    // 面板里只剩高级设置的两个下拉（最大重试次数 / 重试耗尽时）；输入绑定不再是下拉
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
    expect(screen.getByText('taskCenter.editor.field.derived')).toBeTruthy();
  });

  it('把入参类型切成 object 时按 abstract 声明下发', async () => {
    await renderEditor();
    // 第二个入参是 knowledge_id（string）；目标字段也是标量桶，切 object 不会破坏绑定
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click(await screen.findByTitle('object'));

    fireEvent.click(screen.getByText('common.save'));
    await waitFor(() => expect(saveOrchestration).toHaveBeenCalledTimes(1));
    const config = saveOrchestration.mock.calls[0][1];
    // N17 只对非抽象的 object 要求 properties；抽象形态必须显式带 abstract
    expect(config.start.inputs.knowledge_id).toEqual({ type: 'object', required: true, abstract: true });
  });

  it('变量名格式不合法时拦在保存前（与后端引用解析同口径）', async () => {
    await renderEditor();
    // 名字里带空格：后端 `_SEGMENT_RE` 解析 `@task.inputs.<name>` 时会拒，提前拦
    const nameInput = screen.getAllByPlaceholderText('taskCenter.editor.field.varNamePlaceholder')[0];
    fireEvent.change(nameInput, { target: { value: 'bad name' } });

    fireEvent.click(screen.getByText('common.save'));
    await waitFor(() => expect(screen.getAllByText('taskCenter.editor.field.nameInvalid').length).toBeGreaterThan(0));
    expect(saveOrchestration).not.toHaveBeenCalled();
  });

  it('没有业务节点时不允许保存', async () => {
    await renderEditor();
    const canvas = screen.getByTestId('workflow-canvas');
    const parseCard = within(canvas).getByText('文件解析').closest('[data-node-id]') as HTMLElement;
    fireEvent.click(within(parseCard).getByLabelText('taskCenter.editor.action.deleteNode'));

    fireEvent.click(screen.getByText('common.save'));
    await waitFor(() => expect(screen.getByText('taskCenter.editor.step.required')).toBeTruthy());
    expect(saveOrchestration).not.toHaveBeenCalled();
  });
});
