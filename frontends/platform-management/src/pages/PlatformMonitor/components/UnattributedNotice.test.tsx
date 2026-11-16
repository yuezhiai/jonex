// @vitest-environment jsdom
/**
 * 「另有未归因调用」提示的展示口径。
 *
 * 这条提示只做解释，不参与任何 KPI，因此它的渲染规则本身就是口径：
 * - 没有未归因调用 ⇒ **不渲染**（长期挂一条「0 次」会被学会无视）；
 * - 取数失败 ⇒ **不渲染**（解释性提示不该把正常页面弄脏）；
 * - 后端没给占比（非平台管理员）⇒ 只讲绝对量，不拼百分比。
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';

const summary = {
  callCount: 1200,
  totalTokens: 3_400_000,
  firstSeenAt: '2026-11-01T01:00:00Z',
  lastSeenAt: '2026-11-09T02:00:00Z',
  scenes: [],
  shareOfCalls: null as number | null,
  shareOfTokens: null as number | null,
};

let state: { data: typeof summary | null; error: Error | null } = { data: null, error: null };

vi.mock('../../../api/platformMonitor', () => ({
  getUnattributedSummary: vi.fn(),
}));

vi.mock('@/hooks/useAsyncData', () => ({
  default: () => ({ data: state.data, loading: false, error: state.error, reload: vi.fn() }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import UnattributedNotice from './UnattributedNotice';

const props = { window: '24h' as const, refreshKey: 0, manualRefreshKey: 0 };

describe('UnattributedNotice', () => {
  it('无未归因调用 → 不渲染', () => {
    state = { data: { ...summary, callCount: 0 }, error: null };
    const { container } = render(<UnattributedNotice {...props} />);
    expect(container.innerHTML).toBe('');
  });

  it('取数失败 → 不渲染', () => {
    state = { data: null, error: new Error('boom') };
    const { container } = render(<UnattributedNotice {...props} />);
    expect(container.innerHTML).toBe('');
  });

  it('有未归因调用 → 显示提示，且没有占比文案', () => {
    state = { data: summary, error: null };
    render(<UnattributedNotice {...props} />);
    expect(screen.getByText('platformMonitor.unattributed.title')).toBeTruthy();
    expect(screen.getByText('platformMonitor.unattributed.description')).toBeTruthy();
    expect(screen.queryByText(/platformMonitor\.unattributed\.share/)).toBeNull();
  });

  it('平台管理员拿到占比 → 追加占比文案', () => {
    state = { data: { ...summary, shareOfCalls: 16.4, shareOfTokens: 12.5 }, error: null };
    const { container } = render(<UnattributedNotice {...props} />);
    expect(container.textContent).toContain('platformMonitor.unattributed.share');
  });
});
