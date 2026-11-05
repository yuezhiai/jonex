/**
 * 概念对齐区（P1.5b）组件级回归（jsdom + RTL）。
 *
 * 钉住容易静默坏掉的四点：
 * - 四态（loading / empty / error / success）都显式；
 * - 审批只受 `readOnly` 控制（与抽象 schema 是否 active 无关）；
 * - 「生成建议」另受 `suggestDisabled` 控制，且原因**常显**（不是只藏 tooltip）；
 * - 审批成功用回包整行替换（不回退成加载中/清空）。
 *
 * 断言按 i18n key（`react-i18next` mock 成直出键名），不锁中文文案。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';

import DataAlignmentSection from './DataAlignmentSection';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import {
  listAlignments,
  setAlignmentStatus,
  suggestAlignments,
  type OntologyDataAlignment,
} from '@/api/ontologyAlignment';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getDomainKnowledgeDataSources: vi.fn(),
}));

vi.mock('@/api/ontologyAlignment', () => ({
  listAlignments: vi.fn(),
  suggestAlignments: vi.fn(),
  setAlignmentStatus: vi.fn(),
}));

const dsMock = vi.mocked(getDomainKnowledgeDataSources);
const listMock = vi.mocked(listAlignments);
const suggestMock = vi.mocked(suggestAlignments);
const setStatusMock = vi.mocked(setAlignmentStatus);

function row(overrides: Partial<OntologyDataAlignment> = {}): OntologyDataAlignment {
  return {
    id: 'a1',
    doc_type: 'Asset',
    data_source_id: 'ds_1234567890abcdef',
    entity_name: 'Asset',
    status: 'proposed',
    ...overrides,
  };
}

beforeEach(async () => {
  vi.resetAllMocks();
  dsMock.mockResolvedValue([]);
  listMock.mockResolvedValue({ items: [row()] });
  // antd 静态 message holder 预热（同 LineageTab.test.tsx）
  message.success('__holder_warmup__');
  await new Promise((r) => setTimeout(r, 150));
  message.destroy();
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe('DataAlignmentSection', () => {
  it('success：渲染文档实体 ↔ 数据源（名字缺失时缩 id）+ 审批按钮', async () => {
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findAllByText('Asset')).not.toHaveLength(0);
    expect(screen.getByText('ds_12345…')).toBeTruthy();
    expect(screen.getByTitle('ds_1234567890abcdef')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'domainKnowledge.dataAlignment.activate' }))
      .toBeTruthy();
    expect(screen.getByRole('button', { name: 'domainKnowledge.dataAlignment.reject' }))
      .toBeTruthy();
    // 非过期/缺失态不显示 freshness 提示
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessOutdated')).toBeNull();
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessMissing')).toBeNull();
  });

  it('empty：显式「还没有建议」', async () => {
    listMock.mockResolvedValue({ items: [] });
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findByText('domainKnowledge.dataAlignment.empty')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessOutdated')).toBeNull();
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessMissing')).toBeNull();
  });

  it('freshness=seeded：两类 freshness 提示都不出现', async () => {
    listMock.mockResolvedValue({ items: [row()], freshness: 'seeded' });
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    await screen.findAllByText('Asset');
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessOutdated')).toBeNull();
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessMissing')).toBeNull();
  });

  it('error：后端 message 原样透出（不落本地通用文案）', async () => {
    listMock.mockRejectedValueOnce(new Error('知识库不存在'));
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findByText('知识库不存在')).toBeTruthy();
  });

  it('readOnly：审批按钮仍渲染但全部禁用，并显示只读提示', async () => {
    render(<DataAlignmentSection kbId="kb_1" readOnly suggestDisabled />);

    const activate = await screen.findByRole('button', {
      name: 'domainKnowledge.dataAlignment.activate',
    }) as HTMLButtonElement;
    expect(activate.disabled).toBe(true);
    expect(screen.getByText('domainKnowledge.dataAlignment.readOnlyHint')).toBeTruthy();
    // suggestDisabled && !readOnly 才常显原因；只读时不重复刷屏
    expect(screen.queryByText('domainKnowledge.dataAlignment.suggestNeedsActive')).toBeNull();
  });

  it('suggestDisabled（可写但 schema 未 active）：按钮禁用 + 原因常显', async () => {
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled />);

    const suggest = screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.suggest',
    }) as HTMLButtonElement;
    await waitFor(() => expect(suggest.disabled).toBe(true));
    expect(screen.getByText('domainKnowledge.dataAlignment.suggestNeedsActive')).toBeTruthy();
  });

  it('生成建议成功 → 摘要 created/existing/unresolved + 重拉列表', async () => {
    suggestMock.mockResolvedValue({ created: 2, existing: 1, unresolved: 3 });
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    await screen.findAllByText('Asset');
    fireEvent.click(screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.suggest',
    }));

    expect(await screen.findByText(
      'domainKnowledge.dataAlignment.suggestResult({"created":2,"existing":1,"unresolved":3})',
    )).toBeTruthy();
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it('outdated：顶部提示先刷新 TBox；只有 stale === true 的行带「文档类型已不存在」', async () => {
    listMock.mockResolvedValue({
      items: [
        row({ id: 'a1', doc_type: 'Asset', stale: false }),
        row({ id: 'a2', doc_type: 'Gone', stale: true }),
        row({ id: 'a3', doc_type: 'Unknown', stale: null }),
      ],
      freshness: 'outdated',
    });
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findByText('domainKnowledge.dataAlignment.freshnessOutdated'))
      .toBeTruthy();
    // 三态：false / null 都不显示 Tag，只有 true 的行显示（恰好一条）
    expect(screen.getAllByText('domainKnowledge.dataAlignment.staleDocType')).toHaveLength(1);
  });

  it('freshness=missing：显式空态原因，不伪装成「没有建议」', async () => {
    listMock.mockResolvedValue({ items: [], freshness: 'missing' });
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findByText('domainKnowledge.dataAlignment.freshnessMissing'))
      .toBeTruthy();
    // 空态照旧保留（原因说明与「没有建议」并存）
    expect(screen.getByText('domainKnowledge.dataAlignment.empty')).toBeTruthy();
  });

  it('刷新失败：错误态优先，不残留上一次的 freshness 提示', async () => {
    listMock.mockResolvedValueOnce({ items: [row()], freshness: 'outdated' });
    listMock.mockRejectedValueOnce(new Error('刷新失败'));
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    expect(await screen.findByText('domainKnowledge.dataAlignment.freshnessOutdated'))
      .toBeTruthy();
    fireEvent.click(screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.refresh',
    }));

    expect(await screen.findByText('刷新失败')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.dataAlignment.freshnessOutdated')).toBeNull();
  });

  it('审批成功用回包整行替换（状态标签立即变化，不退回 loading）', async () => {
    setStatusMock.mockResolvedValue(row({ status: 'active' }));
    render(<DataAlignmentSection kbId="kb_1" readOnly={false} suggestDisabled={false} />);

    await screen.findAllByText('Asset');
    fireEvent.click(screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.activate',
    }));

    await waitFor(() =>
      expect(setStatusMock).toHaveBeenCalledWith('kb_1', 'a1', 'active'));
    // 分组头 + 行内状态各一处：用 findAll 避开「多个匹配」
    expect(await screen.findAllByText('domainKnowledge.dataAlignment.statusActive'))
      .not.toHaveLength(0);
    // active 行不再显示「启用」，但仍应显示「拒绝」
    expect(screen.queryByRole('button', {
      name: 'domainKnowledge.dataAlignment.activate',
    })).toBeNull();
  });
});
