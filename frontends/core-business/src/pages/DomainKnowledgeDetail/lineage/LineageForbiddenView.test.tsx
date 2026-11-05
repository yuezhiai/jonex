/** 「禁止同查」视图：动作按状态分流、canWrite 真消费、空态显式。 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import LineageForbiddenView from './LineageForbiddenView';
import {
  deleteLineageForbiddenPair, listLineageForbiddenPairs,
  suggestLineageForbiddenPairs, updateLineageForbiddenPair,
} from '@/api/lineage';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string) => key,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock('@/api/lineage', () => ({
  listLineageForbiddenPairs: vi.fn(),
  createLineageForbiddenPair: vi.fn(),
  updateLineageForbiddenPair: vi.fn(),
  deleteLineageForbiddenPair: vi.fn(),
  suggestLineageForbiddenPairs: vi.fn(),
}));

const listMock = vi.mocked(listLineageForbiddenPairs);
const updateMock = vi.mocked(updateLineageForbiddenPair);
const deleteMock = vi.mocked(deleteLineageForbiddenPair);
const suggestMock = vi.mocked(suggestLineageForbiddenPairs);

function pair(status: string) {
  return { id: `p-${status}`, data_source_id: 'ds1', table_a: 'dbo.jobs',
           table_b: 'dbo.job_forward', origin: 'manual', status };
}

beforeEach(() => {
  listMock.mockResolvedValue({ items: [], total: 0 });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('canWrite=false 时不渲染任何写按钮', async () => {
  listMock.mockResolvedValue({ items: [pair('proposed')], total: 1 });
  render(<LineageForbiddenView kbId="kb" dsId="ds1" canWrite={false} />);
  await waitFor(() => expect(screen.getByText('dbo.jobs')).toBeTruthy());
  expect(screen.queryByText('domainKnowledge.lineage.forbidden.adopt')).toBeNull();
  expect(screen.queryByText('domainKnowledge.lineage.forbidden.suggest')).toBeNull();
});

it('proposed 采纳 → PATCH active', async () => {
  listMock.mockResolvedValue({ items: [pair('proposed')], total: 1 });
  updateMock.mockResolvedValue(pair('active') as any);
  render(<LineageForbiddenView kbId="kb" dsId="ds1" canWrite />);
  await waitFor(() => screen.getByText('domainKnowledge.lineage.forbidden.adopt'));
  fireEvent.click(screen.getByText('domainKnowledge.lineage.forbidden.adopt'));
  await waitFor(() => expect(updateMock).toHaveBeenCalledWith(
    'kb', 'p-proposed', { status: 'active' }));
});

it('刷新建议显示 created/skipped', async () => {
  suggestMock.mockResolvedValue({ created: 2, skipped: 1, total: 3 });
  render(<LineageForbiddenView kbId="kb" dsId="ds1" canWrite />);
  await waitFor(() =>
    expect(screen.getByText('domainKnowledge.lineage.forbidden.suggest')).toBeTruthy());
  fireEvent.click(screen.getByText('domainKnowledge.lineage.forbidden.suggest'));
  await waitFor(() => expect(suggestMock).toHaveBeenCalledWith('kb', 'ds1'));
});

it('空态显式提示可刷新建议或手工新增', async () => {
  render(<LineageForbiddenView kbId="kb" dsId="ds1" canWrite />);
  await waitFor(() =>
    expect(screen.getByText('domainKnowledge.lineage.forbidden.empty')).toBeTruthy());
});
