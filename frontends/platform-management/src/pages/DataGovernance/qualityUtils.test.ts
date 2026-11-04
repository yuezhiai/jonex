import { describe, expect, it } from 'vitest';

import {
  buildQualityAssetId,
  buildSqlText,
  formatMetricValue,
  isIssueActionAllowed,
  issueEventLabelKey,
  issueStatusTransitions,
  metricLabelKey,
  parseQualityAssetId,
  ruleSummary,
  splitResultsJson,
  sqlFilename,
  type QualityTranslate,
} from './qualityUtils';

const labels: Record<string, string> = {
  'dataGovernance.quality.metric.missingValues': 'Missing values',
  'dataGovernance.quality.metric.rowCount': 'Row count',
  'dataGovernance.quality.checkType.sql': 'SQL',
  'dataGovernance.quality.checkType.library': 'Library',
  'dataGovernance.quality.rule.valueSeparator': ', ',
};

const translate: QualityTranslate = (key, params) => {
  if (key === 'dataGovernance.quality.rule.missingValues') {
    return `${params?.label} (${params?.values})`;
  }
  return labels[key] ?? key;
};

describe('qualityUtils', () => {
  it('规则摘要：library 带缺失值样本，非 library 显示本地化类型', () => {
    expect(ruleSummary({
      checkType: 'library',
      definition: { metric: 'missingValues', missingValues: ['', 'N/A', 'x', 'y'] },
    }, translate)).toBe('Missing values (, N/A, x…)');
    expect(ruleSummary({ checkType: 'sql', definition: {} }, translate)).toBe('SQL');
    expect(ruleSummary({ checkType: 'library', definition: { metric: 'rowCount' } }, translate))
      .toBe('Row count');
    // 未知指标退回原始编码，不显示裸 key。
    expect(ruleSummary({ checkType: 'library', definition: { metric: 'customMetric' } }, translate))
      .toBe('customMetric');
  });

  it('只有管理员或当前指派人可操作工单', () => {
    expect(isIssueActionAllowed({ assigneeId: '9' }, '9', false)).toBe(true);
    expect(isIssueActionAllowed({ assigneeId: '9' }, '1', false)).toBe(false);
    expect(isIssueActionAllowed({ assigneeId: null }, '1', true)).toBe(true);
  });

  it('状态机目标与后端一致', () => {
    expect(issueStatusTransitions('open')).toEqual(['assigned', 'fixing']);
    expect(issueStatusTransitions('pending_verify')).toEqual(['fixing']);
    expect(issueStatusTransitions('closed')).toEqual([]);
  });

  it('结果 JSON：裸数组 / results 包装 / 非法输入', () => {
    expect(splitResultsJson('[{"rule_id":"r1","status":"passed"}]')).toEqual({
      ok: true,
      items: [{ ruleId: 'r1', status: 'passed' }],
    });
    expect(splitResultsJson('{"results":[{"ruleId":"r2","status":"failed","metric_value":3}]}'))
      .toEqual({
        ok: true,
        items: [{ ruleId: 'r2', status: 'failed', metricValue: 3 }],
      });
    expect(splitResultsJson('')).toEqual({ ok: false, error: 'empty' });
    expect(splitResultsJson('{')).toEqual({ ok: false, error: 'invalid_json' });
    expect(splitResultsJson('{"x":1}')).toEqual({ ok: false, error: 'not_array' });
    expect(splitResultsJson('[{"status":"passed"}]')).toEqual({
      ok: false, error: 'invalid_item',
    });
  });

  it('格式化指标值', () => {
    expect(formatMetricValue(null)).toBe('-');
    expect(formatMetricValue(0)).toBe('0');
    expect(formatMetricValue({ a: 1 })).toBe('{"a":1}');
  });

  it('资产 ID 解析 / 拼接与后端同口径', () => {
    expect(parseQualityAssetId('ds:dbo.orders.amount', 'column')).toEqual({
      dataSourceId: 'ds', table: 'dbo.orders', column: 'amount',
    });
    expect(parseQualityAssetId('ds:dbo.orders', 'table')).toEqual({
      dataSourceId: 'ds', table: 'dbo.orders', column: null,
    });
    expect(parseQualityAssetId('ds:dbo.orders.amount', 'table')).toEqual({
      dataSourceId: 'ds', table: 'dbo.orders.amount', column: null,
    });
    expect(parseQualityAssetId('bad', 'column')).toEqual({
      dataSourceId: '', table: '', column: null,
    });
    expect(buildQualityAssetId('ds', 'dbo.orders')).toBe('ds:dbo.orders');
    expect(buildQualityAssetId('ds', 'dbo.orders', 'amount')).toBe('ds:dbo.orders.amount');
  });

  it('巡检 SQL 文本与下载文件名', () => {
    expect(buildSqlText([
      { name: 'r1', sql: 'SELECT 1' },
      { name: 'r2', sql: null },
    ], '<external>')).toBe('-- r1\nSELECT 1\n\n-- r2\n<external>');

    expect(sqlFilename('run-1')).toBe('quality-run-run-1.sql');
    expect(sqlFilename('run/1 2')).toBe('quality-run-run-1-2.sql');
    expect(sqlFilename(null)).toBe('quality-rules.sql');
    expect(sqlFilename('')).toBe('quality-rules.sql');
  });

  it('动态标签 key 与后端枚举对齐', () => {
    expect(metricLabelKey('nullValues')).toBe('dataGovernance.quality.metric.nullValues');
    expect(issueEventLabelKey('status_change'))
      .toBe('dataGovernance.quality.issueEvent.status_change');
  });
});
