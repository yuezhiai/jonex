/**
 * 「添加数据源」弹窗：数据库接入的**默认名称**。
 *
 * 现场问题：选“数据库接入”后名称被自动填成接入方式名（数据库接入），连着建三个库
 * 就出现三条同名数据源，ETL 的目标数据源下拉里全是“数据库接入”，分不清是哪台库。
 * 期望：选中连接后，若名称仍是默认值（或为空），自动带出**连接名**；用户已经敲过
 * 名字时不覆盖。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import AddDataSourceModal from './AddDataSourceModal';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));

vi.mock('@/api/dataSource', () => ({
  listAccessMethods: vi.fn(async () => [{
    id: 'dam_database', name: '数据库接入', accessType: 'database',
    description: '', status: 'active',
  }]),
  listConnections: vi.fn(async () => [{
    id: 'c1', name: '集团汇总库', access_type: 'database',
    config_json: { engine: 'postgresql', database: 'wte_dw' }, status: 'active',
  }]),
  createDataSource: vi.fn(),
}));

const METHOD_LABEL = 'domainKnowledge.dataSourceType.database';
const CONN_LABEL = '集团汇总库（postgresql / wte_dw）';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderModal() {
  render(
    <AddDataSourceModal
      open
      kbId="kb_1"
      spaceId="space_demo"
      existingTypes={[]}
      onClose={vi.fn()}
      onCreated={vi.fn()}
    />,
  );
}

async function pick(comboboxIndex: number, title: string) {
  fireEvent.mouseDown(screen.getAllByRole('combobox')[comboboxIndex]);
  fireEvent.click(await screen.findByTitle(title));
}

describe('AddDataSourceModal 默认名称', () => {
  it('选中连接后，默认名称从「数据库接入」换成连接名', async () => {
    renderModal();
    await pick(0, METHOD_LABEL);

    const nameInput = (await screen.findByLabelText('domainKnowledge.dataSourceName')) as HTMLInputElement;
    expect(nameInput.value).toBe(METHOD_LABEL);

    await pick(1, CONN_LABEL);
    expect(nameInput.value).toBe('集团汇总库');
  });

  it('用户已经敲过的名称不被连接名覆盖', async () => {
    renderModal();
    await pick(0, METHOD_LABEL);

    const nameInput = (await screen.findByLabelText('domainKnowledge.dataSourceName')) as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: '我的汇总库' } });

    await pick(1, CONN_LABEL);
    expect(nameInput.value).toBe('我的汇总库');
  });
});
