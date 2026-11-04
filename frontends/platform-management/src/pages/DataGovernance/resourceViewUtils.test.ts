import { describe, expect, it } from 'vitest';
import {
  UNASSIGNED_DOMAIN,
  UNASSIGNED_DOMAIN_KEY,
  UNOWNED_OWNER,
  UNOWNED_OWNER_KEY,
  anchorState,
  bindingSummary,
  coveragePercent,
  domainLabel,
  ownerLabel,
  sensitivityTotals,
} from './resourceViewUtils';

describe('domainLabel / ownerLabel', () => {
  it('未分配业务域走 i18n 键，其余原样返回', () => {
    expect(domainLabel(UNASSIGNED_DOMAIN)).toBe(UNASSIGNED_DOMAIN_KEY);
    expect(domainLabel('')).toBe(UNASSIGNED_DOMAIN_KEY);
    expect(domainLabel(null)).toBe(UNASSIGNED_DOMAIN_KEY);
    expect(domainLabel(undefined)).toBe(UNASSIGNED_DOMAIN_KEY);
    expect(domainLabel(' Sales ')).toBe('Sales');
  });

  it('owner 优先 display_name，未指定责任人走 i18n 键', () => {
    expect(ownerLabel('u1', 'Bob')).toBe('Bob');
    expect(ownerLabel('u1')).toBe('u1');
    expect(ownerLabel('u1', '  ')).toBe('u1');
    expect(ownerLabel(UNOWNED_OWNER, UNOWNED_OWNER)).toBe(UNOWNED_OWNER_KEY);
    expect(ownerLabel(null)).toBe(UNOWNED_OWNER_KEY);
    expect(ownerLabel('')).toBe(UNOWNED_OWNER_KEY);
  });
});

describe('sensitivityTotals', () => {
  it('逐档求和，缺失 / 非法值按 0', () => {
    const total = sensitivityTotals([
      { sensitivity: { public: 2, secret: 1 } },
      { sensitivity: { public: 1, unclassified: 3 } },
      null,
      undefined,
      {},
    ]);
    expect(total).toEqual({
      public: 3,
      internal: 0,
      confidential: 0,
      secret: 1,
      unclassified: 3,
    });
  });

  it('空列表回全 0', () => {
    expect(sensitivityTotals([])).toEqual({
      public: 0,
      internal: 0,
      confidential: 0,
      secret: 0,
      unclassified: 0,
    });
  });
});

describe('coveragePercent', () => {
  it('null / 非数显示破折号', () => {
    expect(coveragePercent(null)).toBe('—');
    expect(coveragePercent(undefined)).toBe('—');
    expect(coveragePercent(Number.NaN)).toBe('—');
  });

  it('0..1 比率转百分比，整数不带小数点', () => {
    expect(coveragePercent(0)).toBe('0%');
    expect(coveragePercent(0.5)).toBe('50%');
    expect(coveragePercent(1)).toBe('100%');
    expect(coveragePercent(0.826)).toBe('82.6%');
  });
});

describe('anchorState', () => {
  it('degraded 优先于 unresolved', () => {
    expect(anchorState({
      degraded: true,
      unresolved: true,
      linked: [],
    })).toBe('degraded');
  });

  it('无文档 / null 视为 unresolved', () => {
    expect(anchorState({
      degraded: false,
      unresolved: true,
      linked: [],
    })).toBe('unresolved');
    expect(anchorState({
      degraded: false,
      unresolved: false,
      linked: [],
    })).toBe('unresolved');
    expect(anchorState(null)).toBe('unresolved');
    expect(anchorState(undefined)).toBe('unresolved');
  });

  it('有链接且无降级 = linked', () => {
    expect(anchorState({
      degraded: false,
      unresolved: false,
      linked: [{
        knowledgeBaseId: 'kb1',
        docType: '订单',
        entityName: 'Orders',
        entityCount: 1,
        truncated: false,
        attributes: {},
        documents: [{ id: 'd1', name: '订单.pdf', status: 'ready' }],
      }],
    })).toBe('linked');
  });
});

describe('bindingSummary', () => {
  it('标准 + 资产一行摘要', () => {
    expect(bindingSummary({
      standardKind: 'data_element',
      standardCode: 'customer.name',
      assetType: 'column',
      assetId: 'ds:dbo.t.c',
    })).toBe('data_element:customer.name → ds:dbo.t.c');
  });

  it('缺字段用占位符，不抛错', () => {
    expect(bindingSummary(null)).toBe('');
    expect(bindingSummary({
      standardKind: 'term',
      standardCode: '',
      assetType: 'table',
      assetId: '',
    })).toBe('term → —');
    expect(bindingSummary({
      standardKind: 'term',
      standardCode: 'order',
      assetType: 'table',
      assetId: '',
    })).toBe('term:order → —');
  });
});
