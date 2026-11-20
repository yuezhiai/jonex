// @vitest-environment jsdom
/**
 * 「结果」页签统计卡：Neo4j 降级时实例数 / 关系数显示「—」而不是 0，
 * 源文件数照常；resultStats 为空时仍走原有 '--' 占位。
 */
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({ id: 'kb-1' }),
}));

import type { OntologyStatistics } from '@/types/domainKnowledge';
import ResultTab from './ResultTab';

const STATS: OntologyStatistics = {
  knowledge_base_id: 'kb-1',
  knowledge_base_name: '测试库',
  source_file_count: 4,
  ontology_instance_count: 9,
  ontology_relation_count: 6,
};

const DEGRADED_STATS: OntologyStatistics = {
  ...STATS,
  ontology_instance_count: 0,
  ontology_relation_count: 0,
  ontology_degraded: true,
};

function renderTab(resultStats: OntologyStatistics | null) {
  return render(
    <ResultTab
      resultStats={resultStats}
      ontologySummaries={[]}
      relationSummaries={[]}
      resultLoading={false}
    />,
  );
}

afterEach(() => {
  cleanup();
});

describe('ResultTab 统计卡', () => {
  it('正常时展示真实计数', () => {
    renderTab(STATS);

    expect(screen.getByText('4')).not.toBeNull();
    expect(screen.getByText('9')).not.toBeNull();
    expect(screen.getByText('6')).not.toBeNull();
    expect(screen.queryByText('compile.assets.graphUnavailable')).toBeNull();
  });

  it('降级时实例数 / 关系数显示 — 与提示，源文件数照常', () => {
    renderTab(DEGRADED_STATS);

    expect(screen.getAllByText('—')).toHaveLength(2);
    // 源文件数（来自 PG）不受降级影响
    expect(screen.getByText('4')).not.toBeNull();
    // 降级值 0 不得作为计数渲染
    expect(screen.queryByText('0')).toBeNull();
    expect(screen.getByText('compile.assets.graphUnavailable')).not.toBeNull();
  });

  it('resultStats 为空时保留 -- 占位', () => {
    renderTab(null);

    expect(screen.getAllByText('--')).toHaveLength(3);
    expect(screen.queryByText('compile.assets.graphUnavailable')).toBeNull();
  });
});
