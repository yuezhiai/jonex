/** 治理页签：四个子视图入口与标签列表取数。 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import GovernanceTab from './GovernanceTab';
import { listGovernanceLabels } from '@/api/governance';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';

const { tMock } = vi.hoisted(() => ({ tMock: (key: string) => key }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock('@/api/domainKnowledge', () => ({
  getDomainKnowledgeDataSources: vi.fn(),
}));
vi.mock('@/api/governance', () => ({
  listGovernanceLabels: vi.fn(),
  createGovernanceLabel: vi.fn(),
  updateGovernanceLabel: vi.fn(),
  deleteGovernanceLabel: vi.fn(),
  listGovernanceTags: vi.fn(),
  createGovernanceTag: vi.fn(),
  updateGovernanceTag: vi.fn(),
  deleteGovernanceTag: vi.fn(),
  listGovernanceClearance: vi.fn(),
  setGovernanceClearance: vi.fn(),
  listGovernancePolicies: vi.fn(),
  createGovernancePolicy: vi.fn(),
  updateGovernancePolicy: vi.fn(),
  deleteGovernancePolicy: vi.fn(),
  listGovernanceAudit: vi.fn(),
  evaluateGovernance: vi.fn(),
  listMetricCallApprovals: vi.fn(),
  requestMetricCallApproval: vi.fn(),
  approveMetricCall: vi.fn(),
  suspendMetricCall: vi.fn(),
}));

const listLabels = vi.mocked(listGovernanceLabels);
const listSources = vi.mocked(getDomainKnowledgeDataSources);

beforeEach(() => {
  listLabels.mockResolvedValue({ items: [], total: 0 });
  listSources.mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('渲染四个子视图入口并加载标签', async () => {
  render(<GovernanceTab kbId="kb1" canWrite={false} />);
  expect(screen.getByText('domainKnowledge.governance.viewLabels')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.viewPolicies')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.viewClearance')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.viewAudit')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.viewScan')).toBeTruthy();
  expect(screen.getByText('domainKnowledge.governance.viewApprovals')).toBeTruthy();
  await waitFor(() => expect(listLabels).toHaveBeenCalledWith('kb1'));
});

it('空标签时显示空态', async () => {
  render(<GovernanceTab kbId="kb1" canWrite={false} />);
  await waitFor(() =>
    expect(screen.getByText('domainKnowledge.governance.labelsEmpty')).toBeTruthy());
});
