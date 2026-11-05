// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import LedgersTab from './LedgersTab';

const { getLedgers } = vi.hoisted(() => ({ getLedgers: vi.fn() }));

vi.mock('../../api/evolution', () => ({ getLedgers }));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false, media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ledger = (items: Record<string, unknown>[]) => ({
  path: 'db://evolution_ledgers/test',
  exists: true,
  mtime: null,
  items,
});

describe('LedgersTab · relation 台账行', () => {
  it('relation 行渲染 source.col → target.col + 描述，query_rule 行保持 target_code/expression', async () => {
    getLedgers.mockResolvedValue({
      alias: ledger([]),
      query_rule: ledger([
        {
          kind: 'relation',
          name: 'REL_JOBS_JOB_ID_USERS_USER_ID',
          source: 'Jobs', source_column: 'job_id',
          target: 'Users', target_column: 'user_id',
          description: '演化补边',
          kb: 'kb_demo', ds_id: 'ds_1',
          version_before: 1, version_after: 2, verified: true,
          at: '2026-10-25T10:00:00',
        },
        {
          target_code: 'Orders.amount',
          expression: 'SUM(o.amount)',
          kb: 'kb_demo', ds_id: 'ds_1',
          version_before: 1, version_after: 2, verified: true,
          at: '2026-10-25T10:00:01',
        },
      ]),
      rejects: ledger([]),
    });

    render(<LedgersTab />);

    expect(await screen.findByText('Jobs.job_id → Users.user_id')).toBeTruthy();
    expect(screen.getByText('演化补边')).toBeTruthy();
    expect(screen.getByText('Orders.amount')).toBeTruthy();
    expect(screen.getByText('SUM(o.amount)')).toBeTruthy();
  });

  it('constraint 行渲染 op + name（update 行带表达式）', async () => {
    getLedgers.mockResolvedValue({
      alias: ledger([]),
      query_rule: ledger([
        {
          kind: 'constraint',
          op: 'update',
          name: 'query_rule_Orders_amount',
          expression: 'SUM(o.amount * 2)',
          before: { expression: 'SUM(o.amount)' },
          kb: 'kb_demo', ds_id: 'ds_1',
          version_before: 1, version_after: 2, verified: true,
          at: '2026-10-25T10:00:00',
        },
        {
          kind: 'constraint',
          op: 'disable',
          name: 'query_rule_Orders_status',
          before: { enabled: true },
          kb: 'kb_demo', ds_id: 'ds_1',
          version_before: 2, version_after: 3, verified: true,
          at: '2026-10-25T10:00:01',
        },
      ]),
      rejects: ledger([]),
    });

    render(<LedgersTab />);

    expect(await screen.findByText('update')).toBeTruthy();
    expect(screen.getByText('query_rule_Orders_amount')).toBeTruthy();
    expect(screen.getByText('SUM(o.amount * 2)')).toBeTruthy();
    expect(screen.getByText('disable')).toBeTruthy();
    expect(screen.getByText('query_rule_Orders_status')).toBeTruthy();
  });

  it('mapping 行渲染 op + target_code + 物理三元组', async () => {
    getLedgers.mockResolvedValue({
      alias: ledger([
        {
          kind: 'mapping',
          op: 'update',
          target_code: '赛道.编号',
          data_source_id: 'ds_1',
          entity_name: 'Circuits',
          attribute_name: 'circuitid',
          before: { filter: 'old' },
          after: { filter: 'new' },
          kb: 'kb_demo',
          version_before: 1, version_after: 2, verified: true,
          at: '2026-10-25T10:00:00',
        },
      ]),
      query_rule: ledger([]),
      rejects: ledger([]),
    });

    render(<LedgersTab />);

    expect(await screen.findByText('update')).toBeTruthy();
    expect(screen.getByText('赛道.编号')).toBeTruthy();
    // 物理三元组：源 / 实体.列
    expect(screen.getByText(/ds_1 · Circuits\.circuitid/)).toBeTruthy();
  });

  it('tool_schema 行渲染工具名与描述变化（不是 -）', async () => {
    getLedgers.mockResolvedValue({
      alias: ledger([
        {
          kind: 'tool_schema',
          op: 'update',
          name: 'sample_values',
          before: { description: '旧描述' },
          description: '新描述',
          kb: 'kb_demo',
          version_before: 1, version_after: 2, verified: true,
          at: '2026-10-25T10:00:00',
        },
        {
          kind: 'tool_schema',
          op: 'rollback',
          name: 'list_related',
          description: '基础描述',
          kb: 'kb_demo',
          version_before: 2, version_after: 3, verified: false,
          at: '2026-10-25T10:00:01',
        },
      ]),
      query_rule: ledger([]),
      rejects: ledger([]),
    });

    render(<LedgersTab />);

    expect(await screen.findByText('sample_values')).toBeTruthy();
    expect(screen.getByText('旧描述 → 新描述')).toBeTruthy();
    expect(screen.getByText('list_related')).toBeTruthy();
    expect(screen.getByText('基础描述')).toBeTruthy();
    expect(screen.getByText('rollback')).toBeTruthy();
  });
});
