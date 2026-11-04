/** 治理 → 指标调用审批（W4.8 UI）。 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import ApprovalsView from './ApprovalsView';
import { listMetricCallApprovals } from '@/api/governance';

const { tMock } = vi.hoisted(() => ({ tMock: (key: string) => key }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock('@/api/governance', () => ({
  listMetricCallApprovals: vi.fn(),
  requestMetricCallApproval: vi.fn(),
  approveMetricCall: vi.fn(),
  suspendMetricCall: vi.fn(),
}));

const listApprovals = vi.mocked(listMetricCallApprovals);

beforeEach(() => {
  listApprovals.mockResolvedValue({ items: [] });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('加载并渲染审批记录', async () => {
  listApprovals.mockResolvedValue({
    items: [{
      id: 'a1', knowledge_base_id: 'kb1', metric_name: '吨垃圾发电量',
      status: 'pending', requested_by: 'u1', decided_by: null,
      reason: '投产验证', created_at: '2026-11-02T01:00:00',
      updated_at: '2026-11-02T01:00:00',
    }],
  });
  render(<ApprovalsView kbId="kb1" canWrite />);
  await waitFor(() => expect(listApprovals).toHaveBeenCalledWith('kb1', undefined));
  expect(screen.getByText('吨垃圾发电量')).toBeTruthy();
  expect(screen.getAllByText('domainKnowledge.governance.approvalStatus.pending').length).toBeGreaterThan(0);
  expect(screen.getByText('domainKnowledge.governance.approvalApprove')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.approvalSuspend')).toBeTruthy();
});

it('空态', async () => {
  render(<ApprovalsView kbId="kb1" canWrite />);
  await waitFor(() =>
    expect(screen.getByText('domainKnowledge.governance.approvalEmpty')).toBeTruthy());
});

it('只读时申请入口禁用', async () => {
  render(<ApprovalsView kbId="kb1" canWrite={false} />);
  const button = screen.getByText('domainKnowledge.governance.approvalRequest')
    .closest('button');
  expect(button?.hasAttribute('disabled')).toBe(true);
  expect(screen.getByText('domainKnowledge.governance.approvalReadOnly')).toBeTruthy();
});

it('加载失败显示错误与重试', async () => {
  listApprovals.mockRejectedValue(new Error('boom'));
  render(<ApprovalsView kbId="kb1" canWrite />);
  await waitFor(() => expect(screen.getByText('boom')).toBeTruthy());
  expect(screen.getByText('common.retry')).toBeTruthy();
});
