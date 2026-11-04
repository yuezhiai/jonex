/**
 * [A] 血缘解释块的渲染钉死（jsdom + RTL）。
 *
 * 绑定要求（Task 7 review 的 Low 缺口 + 2026-10-21 默认收起）：
 * - 默认收起：只有标题行，条目内容不渲染；点击标题后展开；
 * - `unknown: true` → 展开后必须渲染**显式提示**（读不到 ≠ 没有）——删掉提示行本测试即红；
 * - `truncated: true` → 必须渲染截断提示（标题行常驻）；
 * - 普通条目 → 展开后实体 / 表 / 上下游端点都渲染；
 * - 空块 / 缺席块 → 什么都不渲染（旧后端与非本体路径零回归）。
 *
 * i18n 与既有组件测试（LineageTab.test.tsx / FocusGenerateModal.test.tsx）同款：
 * `react-i18next` mock 成直出键名，不锁具体中文文案。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string) => key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

import DataLineageView from './DataLineageView';
import type { DataLineageBlock, DataLineageItem } from '@/types/knowledgeSearch';

const NS = 'domainKnowledge.search.dataLineage';

function item(overrides: Partial<DataLineageItem> = {}): DataLineageItem {
  return {
    entity_type: 'Job',
    data_source_id: 'ds_assets',
    table: 'dbo.jobs',
    upstream: [],
    downstream: [],
    unknown: false,
    ...overrides,
  };
}

afterEach(() => cleanup());

/** 默认收起：点标题行展开。 */
function expand(): void {
  fireEvent.click(screen.getByText(`${NS}.title`));
}

describe('DataLineageView', () => {
  it('默认收起：只渲染标题行，条目内容在点击后才出现', () => {
    render(<DataLineageView block={{ items: [item()] }} />);

    expect(screen.getByText(`${NS}.title`)).toBeTruthy();
    expect(screen.queryByText('dbo.jobs')).toBeNull();
    expand();
    expect(screen.getByText('dbo.jobs')).toBeTruthy();
  });

  it('unknown=true → 渲染显式提示（读不到血缘不代表没有），而非留空', () => {
    render(<DataLineageView block={{ items: [item({ unknown: true })] }} />);
    expand();

    // 显式提示必须在场 —— 删掉 unknown 渲染行（DataLineageView.tsx:141-143）本断言即红
    expect(screen.getByText(`${NS}.unknown`)).toBeTruthy();
    // 提示是附加行：条目本身（表名）照常渲染
    expect(screen.getByText('dbo.jobs')).toBeTruthy();
    // 未截断 → 不得出现截断提示
    expect(screen.queryByText(`${NS}.truncated`)).toBeNull();
  });

  it('truncated=true → 渲染截断提示', () => {
    render(<DataLineageView block={{ items: [item()], truncated: true }} />);

    // 删掉 truncated 渲染行（DataLineageView.tsx:84）本断言即红
    expect(screen.getByText(`${NS}.truncated`)).toBeTruthy();
    // 无 unknown → 不得凭空渲染「读不到」提示
    expect(screen.queryByText(`${NS}.unknown`)).toBeNull();
  });

  it('普通条目 → 实体 / 表 / 上游端点 / 下游端点都渲染', () => {
    render(
      <DataLineageView
        block={{
          items: [
            item({
              upstream: [{ endpoint: 'dbo.src', column: 'id' }],
              downstream: [{ endpoint: 'dbo.jobs_out', column: 'job_id' }],
            }),
          ],
        }}
      />,
    );
    expand();

    expect(screen.getByText(`${NS}.title`)).toBeTruthy();
    expect(screen.getByText('Job')).toBeTruthy();
    expect(screen.getByText('dbo.jobs')).toBeTruthy();
    expect(screen.getByText(`${NS}.upstream`)).toBeTruthy();
    expect(screen.getByText(`${NS}.downstream`)).toBeTruthy();
    // 端点渲染为「表.列」全串
    expect(screen.getByText('dbo.src.id')).toBeTruthy();
    expect(screen.getByText('dbo.jobs_out.job_id')).toBeTruthy();
    // 普通条目不带 unknown / truncated 提示
    expect(screen.queryByText(`${NS}.unknown`)).toBeNull();
    expect(screen.queryByText(`${NS}.truncated`)).toBeNull();
  });

  it('空块 / 缺席块 → 什么都不渲染（旧后端零回归）', () => {
    const absent = render(<DataLineageView />);
    expect(absent.container.firstChild).toBeNull();
    absent.unmount();

    const empty = render(<DataLineageView block={{ items: [] }} />);
    expect(empty.container.firstChild).toBeNull();
    empty.unmount();

    const nulled = render(<DataLineageView block={null} />);
    expect(nulled.container.firstChild).toBeNull();
  });
});
