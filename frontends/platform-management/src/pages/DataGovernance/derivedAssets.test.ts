/**
 * 派生资产（表/列/指标）纯函数测试。
 *
 * 只 import `./utils`（不含 antd / React），保持 node 测试环境轻量；
 * 详情抽屉与血缘组件的 JSX 分支由 typecheck 覆盖。
 */
import { describe, expect, it } from 'vitest';
import type { DgLineage } from './types';
import { derivedLineageCounts, syncStatusText } from './utils';

const t = (key: string) => `T:${key}`;

describe('syncStatusText（派生表详情的同步状态）', () => {
  it('已知 compiled 同步状态走 i18n 词表', () => {
    expect(syncStatusText(t, 'synced')).toBe('T:dataGovernance.syncStatus.synced');
    expect(syncStatusText(t, 'outdated')).toBe('T:dataGovernance.syncStatus.outdated');
    expect(syncStatusText(t, 'apply_failed')).toBe(
      'T:dataGovernance.syncStatus.apply_failed',
    );
  });

  it('未知状态原样展示，不臆造语义', () => {
    expect(syncStatusText(t, 'partial')).toBe('partial');
  });

  it('缺失状态显示占位符', () => {
    expect(syncStatusText(t, null)).toBe('—');
    expect(syncStatusText(t, undefined)).toBe('—');
  });
});

function makeLineage(overrides: Partial<DgLineage> = {}): DgLineage {
  return {
    asset: { assetType: 'table', assetId: 'ds1:jobs', name: 'jobs' },
    nodes: [
      { id: 'ds1:jobs', type: 'table', label: 'jobs', meta: {} },
      { id: 'ds1:job_forward', type: 'table', label: 'job_forward', meta: {} },
    ],
    edges: [
      {
        source: 'ds1:jobs',
        target: 'ds1:job_forward',
        type: 'lineage',
        meta: {},
      },
    ],
    counts: {
      chunks: null,
      entities: 0,
      relations: 0,
      citations: 0,
      nodes: 5,
      edges: 3,
    },
    truncated: { entities: false, relations: false, chunks: false },
    degraded: false,
    unresolved: false,
    ...overrides,
  };
}

describe('derivedLineageCounts（表/列/指标血缘计数）', () => {
  it('优先使用后端 counts.nodes / counts.edges', () => {
    expect(derivedLineageCounts(makeLineage())).toEqual({ nodes: 5, edges: 3 });
  });

  it('counts 缺省时回落到 nodes / edges 数组长度', () => {
    const lineage = makeLineage({
      counts: { chunks: null, entities: 0, relations: 0, citations: 0 },
    });
    expect(derivedLineageCounts(lineage)).toEqual({ nodes: 2, edges: 1 });
  });

  it('0 是合法计数，不被回退逻辑覆盖', () => {
    const lineage = makeLineage({
      counts: { chunks: null, entities: 0, relations: 0, citations: 0, nodes: 0, edges: 0 },
    });
    expect(derivedLineageCounts(lineage)).toEqual({ nodes: 0, edges: 0 });
  });
});
