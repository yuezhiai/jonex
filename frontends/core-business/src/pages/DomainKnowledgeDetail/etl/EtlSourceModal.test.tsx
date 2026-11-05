/**
 * 「新建 ETL 来源」弹窗：命名空间字段的可见性与提交形状。
 *
 * 钉死两件事：
 * - 命名空间**只对 OpenLineage 类型生效**（file 类型不显示、也不提交该键）；
 * - 提交的 `config_json` 与后端 `normalize_etl_config` 同形：
 *   `{data_source_id, etl_kind, namespace?}`，空白命名空间不写键（后端按「不限」处理）。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import EtlSourceModal from './EtlSourceModal';
import { createDataSource } from '@/api/dataSource';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/dataSource', () => ({
  createDataSource: vi.fn(),
}));

const createMock = vi.mocked(createDataSource);

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function renderModal() {
  const props = {
    open: true,
    kbId: 'kb_1',
    databaseSources: [{ id: 'ds_1', name: 'db_demo' } as any],
    onClose: vi.fn(),
    onCreated: vi.fn(),
  };
  render(<EtlSourceModal {...props} />);
  return props;
}

/** antd Select 在 jsdom 里只能靠 mouseDown 展开、再点 option 的 title（列表异步挂载）。 */
async function pickOption(comboboxIndex: number, optionLabel: string) {
  fireEvent.mouseDown(screen.getAllByRole('combobox')[comboboxIndex]);
  fireEvent.click(await screen.findByTitle(optionLabel));
}

const NS_PLACEHOLDER = 'domainKnowledge.etl.namespacePlaceholder';

describe('EtlSourceModal 命名空间', () => {
  it('file 类型不显示命名空间输入', () => {
    renderModal();
    expect(screen.queryByPlaceholderText(NS_PLACEHOLDER)).toBeNull();
  });

  it('OpenLineage 类型显示命名空间，并按归一后的形状提交', async () => {
    createMock.mockResolvedValue({ id: 'ds_new' } as any);
    renderModal();

    await pickOption(0, 'domainKnowledge.etl.kindOpenlineage');

    fireEvent.change(screen.getByPlaceholderText('domainKnowledge.etl.namePlaceholder'), {
      target: { value: '数仓每日血缘' },
    });
    // 目标数据源是「类型」之后的第二个下拉（命名空间是普通输入框，不占 combobox）
    await pickOption(1, 'db_demo');
    fireEvent.change(screen.getByPlaceholderText(NS_PLACEHOLDER), {
      target: { value: '  jonex-wte-etl  ' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.etl.create' }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith({
      knowledge_base_id: 'kb_1',
      access_type: 'etl',
      name: '数仓每日血缘',
      config_json: {
        data_source_id: 'ds_1',
        etl_kind: 'openlineage',
        namespace: 'jonex-wte-etl',
      },
    });
  });
});

describe('EtlSourceModal 平台推送', () => {
  it('OpenMetadata 类型不显示命名空间，显示推送提示', async () => {
    renderModal();
    await pickOption(0, 'domainKnowledge.etl.kindOpenmetadata');

    expect(screen.queryByPlaceholderText(NS_PLACEHOLDER)).toBeNull();
    expect(await screen.findByText('domainKnowledge.etl.pushHintOpenmetadata')).toBeTruthy();
  });

  it('DataHub 类型显示对应推送提示', async () => {
    renderModal();
    await pickOption(0, 'domainKnowledge.etl.kindDatahub');

    expect(await screen.findByText('domainKnowledge.etl.pushHintDatahub')).toBeTruthy();
  });
});


describe('EtlSourceModal 固定目标数据源', () => {
  it('隐藏目标选择器并用固定源提交', async () => {
    createMock.mockResolvedValue({ id: 'ds_new' } as any);
    render(
      <EtlSourceModal
        open
        kbId="kb_1"
        databaseSources={[{ id: 'ds_1', name: 'db_demo' } as any]}
        fixedDataSourceId="ds_1"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    expect(screen.queryByText('domainKnowledge.etl.targetSource')).toBeNull();
    fireEvent.change(screen.getByPlaceholderText('domainKnowledge.etl.namePlaceholder'), {
      target: { value: '订单库血缘' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.etl.create' }));
    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith({
      knowledge_base_id: 'kb_1',
      access_type: 'etl',
      name: '订单库血缘',
      config_json: { data_source_id: 'ds_1', etl_kind: 'file' },
    });
  });
});
