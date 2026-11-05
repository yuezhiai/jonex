import { describe, expect, it } from 'vitest';

import {
  buildIssueListParams,
  buildRuleListParams,
  buildRuleUpdateBody,
  buildRunListParams,
  mapQualityIssue,
  mapQualityRule,
  mapQualityRuleSnapshot,
  mapQualityRun,
} from './quality';

describe('quality api mappers', () => {
  it('规则行 snake→camel，enabled 只认 1/true', () => {
    const rule = mapQualityRule({
      id: 'r1', name: '金额非空', asset_type: 'column',
      asset_id: 'ds:dbo.orders.amount', data_source_id: 'ds', kb_id: null,
      dimension: 'completeness', severity: 'warning', check_type: 'library',
      definition: { metric: 'nullValues' }, threshold: { mustBeLessThan: 1 },
      enabled: 1, contract_id: null, revision: 3,
      created_at: '2026-10-19T00:00:00Z', updated_at: null,
    });
    expect(rule.enabled).toBe(1);
    expect(rule.assetType).toBe('column');
    expect(rule.definition.metric).toBe('nullValues');
    expect(rule.revision).toBe(3);
  });

  it('运行行 rule_ids 缺失时回空数组', () => {
    const run = mapQualityRun({
      id: 'run1', data_source_id: 'ds', status: 'running', total: 2,
    });
    expect(run.ruleIds).toEqual([]);
    expect(run.dialect).toBe('tsql');
    expect(run.total).toBe(2);
  });

  it('问题行保留 revision 与 assignee', () => {
    const issue = mapQualityIssue({
      id: 'i1', rule_id: 'r1', asset_type: 'table', asset_id: 'ds:dbo.t',
      data_source_id: 'ds', severity: 'critical', status: 'fixing',
      assignee_id: '9', title: '失败', revision: 4,
    });
    expect(issue.assigneeId).toBe('9');
    expect(issue.revision).toBe(4);
    expect(issue.status).toBe('fixing');
  });

  it('列表参数只发非空筛选', () => {
    expect(buildRuleListParams({
      page: 2, pageSize: 20, dataSourceId: 'ds', enabled: 0,
      dimension: 'completeness',
    })).toEqual({
      data_source_id: 'ds', enabled: 0, dimension: 'completeness',
      page: 2, page_size: 20,
    });
    expect(buildIssueListParams({ page: 1, pageSize: 20 })).toEqual({
      page: 1, page_size: 20,
    });
  });

  it('更新 body 不带未提供的字段', () => {
    expect(buildRuleUpdateBody({ expectedRevision: 3, name: '改名' })).toEqual({
      expected_revision: 3, name: '改名',
    });
  });

  it('巡检 / 工单筛选参数补齐 data_source_id、asset_type、assignee_id', () => {
    expect(buildRunListParams({
      page: 2, pageSize: 20, dataSourceId: 'ds', status: 'failed',
    })).toEqual({
      data_source_id: 'ds', status: 'failed', page: 2, page_size: 20,
    });
    expect(buildIssueListParams({
      page: 1, pageSize: 20, dataSourceId: 'ds', assetType: 'table', assigneeId: '9',
    })).toEqual({
      assignee_id: '9', data_source_id: 'ds', asset_type: 'table',
      page: 1, page_size: 20,
    });
  });

  it('规则快照 snake→camel 并兜底缺失 / null SQL', () => {
    expect(mapQualityRuleSnapshot([
      {
        rule_id: 'r1', name: '金额非空', asset_type: 'column',
        asset_id: 'ds:dbo.orders.amount', severity: 'critical', sql: 'SELECT 1',
      },
      { rule_id: 'r2', name: '外部', asset_type: 'table', asset_id: 'ds:dbo.t', sql: null },
      'bad-row',
    ])).toEqual([
      {
        ruleId: 'r1', name: '金额非空', assetType: 'column',
        assetId: 'ds:dbo.orders.amount', severity: 'critical', sql: 'SELECT 1',
      },
      {
        ruleId: 'r2', name: '外部', assetType: 'table',
        assetId: 'ds:dbo.t', severity: 'warning', sql: null,
      },
      {
        ruleId: '', name: '', assetType: 'table', assetId: '',
        severity: 'warning', sql: null,
      },
    ]);
    expect(mapQualityRuleSnapshot(undefined)).toEqual([]);
  });
});
