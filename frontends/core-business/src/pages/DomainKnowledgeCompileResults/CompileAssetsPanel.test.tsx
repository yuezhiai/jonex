// @vitest-environment jsdom
/**
 * 全库编译资产面板的错误态回归测试。
 *
 * 缺陷：四个初始化请求 `.catch(() => {})` 静默吞错 → statsData 恒为空 → tabs=[] →
 * 页面只剩空白。普通用户无密级时四个接口全 404 / code=1002，主观感受是「一直无响应」。
 *
 * 覆盖点：
 *   1. statistics 失败 ⇒ 渲染统一错误态（知识库不存在或无权访问）+ 重试按钮，而不是空白；
 *   2. 重试按钮重新发起全部四个请求，成功后错误态消失；
 *   3. entity/relation-types 失败 ⇒ 仅次要错误横幅，tab 照常渲染；
 *   4. feedback-stats 失败 ⇒ 静默降级为 0，不打扰用户；
 *   5. llm-wiki（openkb / jonex_wiki）的 tab 来自 wikiStats，statistics 失败也不清空页面；
 *   6. Neo4j 降级（ontology_degraded / degraded）⇒ 计数位显示 —、顶部横幅提示，不渲染 0。
 */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// t 桩：直接返回 key，断言不依赖中文文案。
// 必须是稳定引用：组件的请求 effect 依赖 [kbId, reloadToken, t]，
// 每次渲染换一个新函数会让 effect 无限重跑（表现为骨架屏迟迟不结束）。
const { translate } = vi.hoisted(() => ({
  translate: (key: string) => key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getOntologyStatistics: vi.fn(),
  getOntologyEntityTypes: vi.fn(),
  getOntologyRelationTypes: vi.fn(),
}));

vi.mock('@/api/knowledgeSearch', () => ({
  getSearchFeedbackStats: vi.fn(),
}));

// 子 tab 全部替身化：本测试只关心面板层的加载 / 错误 / 降级分流，
// 子视图自己的请求与呈现已有各自的测试与错误态。
vi.mock('./OntologyTab', () => ({ default: () => null }));
vi.mock('./RelationTab', () => ({ default: () => null }));
vi.mock('./GraphTab', () => ({ default: () => null }));
vi.mock('./ColumnBrowseTab', () => ({ default: () => null }));
vi.mock('./WikiPageBrowser', () => ({ default: () => null }));
vi.mock('./WikiGraphTab', () => ({ default: () => null }));
vi.mock('./wiki/WikiReaderShell', () => ({ default: () => null }));
vi.mock('./wiki/WikiColumnBoard', () => ({ default: () => null }));
vi.mock('./LikeTab', () => ({ default: () => null }));
vi.mock('./DislikeTab', () => ({ default: () => null }));

import {
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  getOntologyStatistics,
} from '@/api/domainKnowledge';
import { getSearchFeedbackStats } from '@/api/knowledgeSearch';
import type { OntologyStatistics, WikiStats } from '@/types/domainKnowledge';
import CompileAssetsPanel from './CompileAssetsPanel';

const mockStats = vi.mocked(getOntologyStatistics);
const mockEntityTypes = vi.mocked(getOntologyEntityTypes);
const mockRelationTypes = vi.mocked(getOntologyRelationTypes);
const mockFeedback = vi.mocked(getSearchFeedbackStats);

const STATS: OntologyStatistics = {
  knowledge_base_id: 'kb-1',
  knowledge_base_name: '测试库',
  source_file_count: 2,
  ontology_instance_count: 7,
  ontology_relation_count: 3,
};

// Neo4j 不可用：后端 200 + ontology_degraded=true，两个计数被降级为 0
const DEGRADED_STATS: OntologyStatistics = {
  ...STATS,
  ontology_instance_count: 0,
  ontology_relation_count: 0,
  ontology_degraded: true,
};

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  // antd Tabs / rc-resize-observer 在 jsdom 里没有原生 ResizeObserver
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
});

beforeEach(() => {
  mockStats.mockReset().mockResolvedValue(STATS);
  mockEntityTypes.mockReset().mockResolvedValue({ items: [], total: 0 });
  mockRelationTypes.mockReset().mockResolvedValue({ items: [], total: 0 });
  mockFeedback.mockReset().mockResolvedValue({ total: 0, like_count: 0, dislike_count: 0 });
});

afterEach(() => {
  cleanup();
});

const WIKI_STATS: WikiStats = { summaries: 2, concepts: 3, entities: 4 };

function renderPanel(props: Partial<React.ComponentProps<typeof CompileAssetsPanel>> = {}) {
  return render(
    <CompileAssetsPanel kbId="kb-1" kbType="lightrag" wikiStats={WIKI_STATS} {...props} />,
  );
}

describe('CompileAssetsPanel 错误态', () => {
  it('statistics 请求失败时渲染错误态与重试按钮，而不是空白', async () => {
    mockStats.mockRejectedValue(new Error('知识库不存在或无权访问'));

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.loadErrorTitle')).not.toBeNull(),
    );
    // 服务端原始 message 作为副标题，便于排障
    expect(screen.getByText('知识库不存在或无权访问')).not.toBeNull();
    expect(screen.getByRole('button', { name: /common\.retry/ })).not.toBeNull();
    // 错误态下不渲染任何 tab 头
    expect(screen.queryByRole('tab')).toBeNull();
  });

  it('点击重试会重新发起四个请求，成功后恢复正常页签', async () => {
    mockStats.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(STATS);

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.loadErrorTitle')).not.toBeNull(),
    );

    fireEvent.click(screen.getByRole('button', { name: /common\.retry/ }));

    await waitFor(() => expect(mockStats).toHaveBeenCalledTimes(2));
    expect(mockEntityTypes).toHaveBeenCalledTimes(2);
    expect(mockRelationTypes).toHaveBeenCalledTimes(2);
    expect(mockFeedback).toHaveBeenCalledTimes(2);

    await waitFor(() =>
      expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull(),
    );
    expect(screen.getByText('domainKnowledge.ontologyInstances')).not.toBeNull();
  });

  it('实体类型 / 关系类型失败只显示次要错误横幅，tab 照常渲染', async () => {
    mockEntityTypes.mockRejectedValue(new Error('entity types boom'));
    mockRelationTypes.mockRejectedValue(new Error('relation types boom'));

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.partialError')).not.toBeNull(),
    );
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
    expect(screen.getByText('domainKnowledge.ontologyInstances')).not.toBeNull();
    expect(screen.getByText('domainKnowledge.relationInstances')).not.toBeNull();
  });

  it('反馈统计失败静默降级为 0，不显示任何错误提示', async () => {
    mockFeedback.mockRejectedValue(new Error('feedback boom'));

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.ontologyInstances')).not.toBeNull(),
    );
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
    expect(screen.queryByText('compile.assets.partialError')).toBeNull();
  });

  it('openkb：statistics 失败不清空 wiki 页签（计数来自 wikiStats）', async () => {
    mockStats.mockRejectedValue(new Error('404'));

    renderPanel({ kbType: 'openkb' });

    await waitFor(() => expect(screen.getByText('compile.wikiPages')).not.toBeNull());
    expect(screen.getByText('compile.wikiGraph')).not.toBeNull();
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
    // 计数取宿主页下发的 wikiStats 之和
    expect(screen.getByText(String(WIKI_STATS.summaries + WIKI_STATS.concepts + WIKI_STATS.entities))).not.toBeNull();
  });

  it('statistics 降级时计数位显示 — 与降级提示，不渲染 0', async () => {
    mockStats.mockResolvedValue(DEGRADED_STATS);

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.graphUnavailable')).not.toBeNull(),
    );
    // 本体实例 / 关系实例两个计数位都换成 —（布局保留，数值不渲染）
    expect(screen.getAllByText('—')).toHaveLength(2);
    expect(screen.queryByText(String(STATS.ontology_instance_count))).toBeNull();
    expect(screen.queryByText(String(STATS.ontology_relation_count))).toBeNull();
    // 降级值 0 不得出现在任何计数位
    expect(screen.queryAllByText('0')).toHaveLength(0);
    // 整页错误态不出现，tab 照常渲染
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
    expect(screen.getByText('domainKnowledge.ontologyInstances')).not.toBeNull();
  });

  it('实体类型接口 degraded=true 时顶部横幅提示，tab 照常渲染', async () => {
    mockEntityTypes.mockResolvedValue({ items: [], total: 0, degraded: true, degraded_reason: 'graph down' });

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.graphUnavailable')).not.toBeNull(),
    );
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
    expect(screen.getByText('domainKnowledge.ontologyInstances')).not.toBeNull();
    expect(screen.getByText('domainKnowledge.relationInstances')).not.toBeNull();
  });

  it('关系类型接口 degraded=true 时同样走横幅提示', async () => {
    mockRelationTypes.mockResolvedValue({ items: [], total: 0, degraded: true, degraded_reason: 'graph down' });

    renderPanel();

    await waitFor(() =>
      expect(screen.getByText('compile.assets.graphUnavailable')).not.toBeNull(),
    );
  });

  it('jonex_wiki：statistics 失败仍展示 Wiki 界面与列式图谱页签', async () => {
    mockStats.mockRejectedValue(new Error('404'));

    renderPanel({ kbType: 'jonex_wiki' });

    await waitFor(() => expect(screen.getByText('compile.wikiReader')).not.toBeNull());
    expect(screen.getByText('compile.wikiColumns')).not.toBeNull();
    expect(screen.queryByText('compile.assets.loadErrorTitle')).toBeNull();
  });
});
