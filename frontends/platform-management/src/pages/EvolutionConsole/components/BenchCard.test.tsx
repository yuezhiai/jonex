// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import BenchCard, { buildCaseRows } from './BenchCard';
import type { BenchResult } from '../../../api/evolution';

// 这些断言按中文文案写：显式给 bench 相关 key 配中文，其余 key 原样返回
// （与 SmokeCard/GateCard 的 key 透传口径一致）。
vi.mock('react-i18next', () => {
  const messages: Record<string, string> = {
    'evolutionConsole.benchParent': '父本',
    'evolutionConsole.benchCandidate': '候选',
    'evolutionConsole.benchNewRed': '新增红（new_red）',
    'evolutionConsole.benchNewError': '新增报错（new_error）',
    'evolutionConsole.benchSkipped': '语料不足/执行器不可用',
    'evolutionConsole.benchPassed': '通过',
    'evolutionConsole.benchNoNewRed': '无新增红',
    'evolutionConsole.benchNotRun': '未跑',
    'evolutionConsole.benchNotRunHint': '采纳后才会跑完整语料基准',
    // 原因码单独翻译成不含字面量 `new_red` 的文案，避免与标题重复命中。
    'evolutionConsole.benchReason.new_red': '新增红题',
    'evolutionConsole.benchReason.new_error': '新增报错题',
    'evolutionConsole.benchReason.corpus_empty': '语料为空',
    'evolutionConsole.benchRulerCrossed': '跨臂 ruler 变化',
    'evolutionConsole.benchRulerFacet.abstract': '抽象 schema',
    'evolutionConsole.benchRulerFacet.compiled': '编译 schema',
  };
  return {
    useTranslation: () => ({
      t: (key: string, opts?: { defaultValue?: string }) =>
        messages[key] ?? opts?.defaultValue ?? key,
    }),
  };
});

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
afterEach(cleanup);

const passedBench: BenchResult = {
  mode: 'gateway',
  corpus_size: 2,
  parent: {
    passes: 2,
    graded: 2,
    errors: 0,
    cases: [
      { name: 'case_a', outcome: 'pass' },
      { name: 'case_b', outcome: 'pass' },
    ],
  },
  candidate: {
    passes: 2,
    graded: 2,
    errors: 0,
    cases: [
      { name: 'case_a', outcome: 'pass' },
      { name: 'case_b', outcome: 'pass' },
    ],
  },
  new_red: [],
  new_error: [],
  flaky: [],
  reasons: [],
  duration_ms: 1234,
  bench_mode: 'enforce',
};

const failedBench: BenchResult = {
  mode: 'gateway',
  corpus_size: 2,
  parent: {
    passes: 1,
    graded: 2,
    errors: 0,
    cases: [
      { name: 'case_a', outcome: 'pass' },
      { name: 'case_b', outcome: 'fail' },
    ],
  },
  candidate: {
    passes: 0,
    graded: 2,
    errors: 0,
    cases: [
      { name: 'case_a', outcome: 'fail', reason_code: 'wrong_answer' },
      { name: 'case_b', outcome: 'fail', reason_code: 'wrong_answer' },
    ],
  },
  new_red: ['case_a'],
  new_error: [],
  flaky: [],
  reasons: ['new_red'],
  duration_ms: 4321,
  bench_mode: 'enforce',
};

const skippedBench: BenchResult = {
  mode: 'gateway',
  corpus_size: 0,
  parent: null,
  candidate: null,
  new_red: [],
  new_error: [],
  flaky: [],
  reasons: ['corpus_empty'],
  override: true,
  bench_mode: 'enforce',
};

describe('BenchCard', () => {
  it('renders parent → candidate counts and pass state', () => {
    render(<BenchCard bench={passedBench} benchStatus="passed" />);
    // 仓库未装 @testing-library/jest-dom，沿用既有用例的 toBeTruthy 断言。
    expect(screen.getByText(/2\/2/)).toBeTruthy();
    expect(screen.getByText(/父本/)).toBeTruthy();
    // 这几个是通过分支独有的文案：只有 `/2\/2/` 在失败分支也会命中，不足以证明走的是通过态。
    expect(screen.getByText(/无新增红/)).toBeTruthy();
    expect(screen.getByText(/通过/)).toBeTruthy();
  });

  it('renders not-run when bench is null', () => {
    render(<BenchCard bench={null} benchStatus={null} />);
    expect(screen.getByText(/未跑/)).toBeTruthy();
  });

  it('renders new red cases and reasons on failure', () => {
    render(<BenchCard bench={failedBench} benchStatus="failed" />);
    expect(screen.getByText('case_a')).toBeTruthy();
    expect(screen.getByText(/new_red/)).toBeTruthy();
  });

  it('renders skipped with override hint', () => {
    render(<BenchCard bench={skippedBench} benchStatus="skipped" />);
    expect(screen.getByText(/语料/)).toBeTruthy();
  });

  it('treats a case missing from the candidate arm as error, not not-run', () => {
    const partial: BenchResult = {
      ...passedBench,
      parent: {
        passes: 1,
        graded: 2,
        errors: 0,
        cases: [
          { name: 'case_a', outcome: 'pass' },
          { name: 'case_b', outcome: 'fail', reason_code: 'parent_reason' },
        ],
      },
      candidate: {
        passes: 1,
        graded: 1,
        errors: 0,
        cases: [{ name: 'case_a', outcome: 'pass' }],
      },
      new_error: ['case_b'],
      reasons: ['new_error'],
    };
    const row = buildCaseRows(partial).find((item) => item.name === 'case_b');
    // 与后端 compare_arms 同口径：候选臂在、题缺失 → error（new_error）。
    expect(row?.parent).toBe('fail');
    expect(row?.candidate).toBe('error');
    // 候选缺失时不能把父本的原因码挂到候选那一侧。
    expect(row?.reason_code).toBeNull();
  });

  it('renders candidate-only rows with parent "-" and the candidate reason', () => {
    const bench: BenchResult = {
      ...passedBench,
      parent: {
        passes: 1,
        graded: 1,
        errors: 0,
        cases: [{ name: 'case_a', outcome: 'pass', reason_code: 'parent_reason' }],
      },
      candidate: {
        passes: 1,
        graded: 2,
        errors: 0,
        cases: [
          { name: 'case_a', outcome: 'pass' },
          { name: 'case_c', outcome: 'fail', reason_code: 'cand_reason' },
        ],
      },
    };
    const row = buildCaseRows(bench).find((item) => item.name === 'case_c');
    // 父本臂在但没有这题：后端只按父本行对齐，不计成父本 error。
    expect(row?.parent).toBeNull();
    expect(row?.candidate).toBe('fail');
    expect(row?.reason_code).toBe('cand_reason');
  });

  it('renders not-run cells when the whole arm did not run', () => {
    const rows = buildCaseRows({ ...passedBench, candidate: null });
    expect(rows.find((item) => item.name === 'case_b')?.candidate).toBeNull();
  });

  it('候选臂在但本题缺失 → SQL 列不得回落到父本 SQL', () => {
    const partial: BenchResult = {
      ...passedBench,
      parent: {
        passes: 1,
        graded: 1,
        errors: 0,
        cases: [{ name: 'case_b', outcome: 'pass', sql: 'SELECT parent_only' }],
      },
      candidate: {
        passes: 0,
        graded: 0,
        errors: 0,
        cases: [],
      },
      new_error: ['case_b'],
      reasons: ['new_error'],
    };
    const row = buildCaseRows(partial).find((item) => item.name === 'case_b');
    // 候选缺失这题：显示父本 SQL 会让人误以为候选也执行了这条 SQL。
    expect(row?.sql).toBeNull();
  });

  it('父本独有题（候选臂整臂未跑）→ 仍可显示父本 SQL', () => {
    const bench: BenchResult = {
      ...passedBench,
      parent: {
        passes: 1,
        graded: 1,
        errors: 0,
        cases: [{ name: 'case_b', outcome: 'pass', sql: 'SELECT parent_only' }],
      },
      candidate: null,
    };
    const row = buildCaseRows(bench).find((item) => item.name === 'case_b');
    expect(row?.sql).toBe('SELECT parent_only');
  });

  it('渲染跨臂 ruler 变化（ruler_crossed）', () => {
    render(
      <BenchCard
        bench={{ ...failedBench, ruler_crossed: ['compiled'] }}
        benchStatus="failed"
      />,
    );
    expect(screen.getByText(/编译 schema/)).toBeTruthy();
  });
});
