import { describe, expect, it } from 'vitest';
import type { DgDocStats } from './standardTypes';
import {
  displayNameOrId,
  docStatsParts,
  hasScenarioMismatch,
  isUnpublished,
  kbEntryDomainAction,
  loadErrorText,
  pageAfterSizeChange,
  scenarioRefreshMode,
  shouldKeepImpact,
  templateEntryAction,
} from './standardUtils';

describe('hasScenarioMismatch', () => {
  it('两侧都有值且不同 → 不一致', () => {
    expect(
      hasScenarioMismatch({ bindingScenarioId: 's-1', compiledScenarioId: 's-2' }),
    ).toBe(true);
  });

  it('两侧都有值且相同 → 一致', () => {
    expect(
      hasScenarioMismatch({ bindingScenarioId: 's-1', compiledScenarioId: 's-1' }),
    ).toBe(false);
  });

  it('任一侧缺失都不是不一致（没有可比较的两侧）', () => {
    expect(
      hasScenarioMismatch({ bindingScenarioId: null, compiledScenarioId: 's-2' }),
    ).toBe(false);
    expect(
      hasScenarioMismatch({ bindingScenarioId: 's-1', compiledScenarioId: null }),
    ).toBe(false);
    expect(
      hasScenarioMismatch({ bindingScenarioId: null, compiledScenarioId: null }),
    ).toBe(false);
  });
});

describe('isUnpublished', () => {
  it('null 与空串表示未发布', () => {
    expect(isUnpublished(null)).toBe(true);
    expect(isUnpublished('')).toBe(true);
  });

  it('有值表示已发布', () => {
    expect(isUnpublished('2026-10-01T00:00:00Z')).toBe(false);
  });
});

describe('docStatsParts', () => {
  it('把文档统计映射成展示字段（不含 ontologyReady）', () => {
    const stats: DgDocStats = {
      docTotal: 10,
      noTarget: 1,
      notApplied: 2,
      appliedCurrent: 3,
      ontologyReady: 4,
    };
    expect(docStatsParts(stats)).toEqual({
      total: 10,
      noTarget: 1,
      notApplied: 2,
      appliedCurrent: 3,
    });
  });

  it('全 0 也原样映射（0 是真实统计值）', () => {
    const stats: DgDocStats = {
      docTotal: 0,
      noTarget: 0,
      notApplied: 0,
      appliedCurrent: 0,
      ontologyReady: 0,
    };
    expect(docStatsParts(stats)).toEqual({
      total: 0,
      noTarget: 0,
      notApplied: 0,
      appliedCurrent: 0,
    });
  });
});

describe('templateEntryAction', () => {
  it('已选领域 → 只重载领域详情', () => {
    expect(
      templateEntryAction({
        selectedDomainId: 'd-1',
        domainCount: 0,
        domainsError: false,
      }),
    ).toBe('detail');
  });

  it('无缓存且无错误 → 重取领域列表（覆盖切 Tab 打断的情况）', () => {
    expect(
      templateEntryAction({
        selectedDomainId: null,
        domainCount: 0,
        domainsError: false,
      }),
    ).toBe('domains');
  });

  it('已有缓存或已报错 → 不重复请求', () => {
    expect(
      templateEntryAction({
        selectedDomainId: null,
        domainCount: 3,
        domainsError: false,
      }),
    ).toBe('none');
    expect(
      templateEntryAction({
        selectedDomainId: null,
        domainCount: 0,
        domainsError: true,
      }),
    ).toBe('none');
  });
});

describe('kbEntryDomainAction', () => {
  it('无 domainId → 回到领域列表', () => {
    expect(kbEntryDomainAction(null, null)).toBe('list');
    expect(kbEntryDomainAction('d-1', '')).toBe('list');
  });

  it('与当前选中领域相同 → 显式重载（setState 不会触发 effect）', () => {
    expect(kbEntryDomainAction('d-1', 'd-1')).toBe('explicit');
  });

  it('跨领域 → 交给 selectedDomainId effect，避免重复请求', () => {
    expect(kbEntryDomainAction('d-1', 'd-2')).toBe('effect');
    expect(kbEntryDomainAction(null, 'd-2')).toBe('effect');
  });
});

describe('scenarioRefreshMode', () => {
  it('已有内容才静默刷新，不覆盖旧内容', () => {
    expect(scenarioRefreshMode(true)).toBe('silent');
  });

  it('初载中/错误态走阻塞刷新，显示 loading 或错误', () => {
    expect(scenarioRefreshMode(false)).toBe('blocking');
  });
});

describe('shouldKeepImpact', () => {
  const scope = { type: 'object' as const, id: 'o-1' };

  it('旧结果属于当前 scope 才保留', () => {
    expect(
      shouldKeepImpact({ scopeType: 'object', scopeId: 'o-1' }, scope),
    ).toBe(true);
    expect(
      shouldKeepImpact({ scopeType: 'scenario', scopeId: 'o-1' }, scope),
    ).toBe(false);
    expect(
      shouldKeepImpact({ scopeType: 'object', scopeId: 'o-2' }, scope),
    ).toBe(false);
  });

  it('没有旧结果时不保留', () => {
    expect(shouldKeepImpact(null, scope)).toBe(false);
  });
});

describe('pageAfterSizeChange', () => {
  it('改每页条数 → 回到第 1 页', () => {
    expect(pageAfterSizeChange(20, 10, 50)).toBe(1);
  });

  it('只翻页 → 使用目标页', () => {
    expect(pageAfterSizeChange(20, 3, 20)).toBe(3);
  });
});

describe('displayNameOrId', () => {
  it('优先名称，其次 id，都没有才占位', () => {
    expect(displayNameOrId('金融', 'd-1')).toBe('金融');
    expect(displayNameOrId(null, 'd-1')).toBe('d-1');
    expect(displayNameOrId('  ', 'd-1')).toBe('d-1');
    expect(displayNameOrId(null, null)).toBe('—');
  });
});

describe('loadErrorText', () => {
  const translate = (key: string) => `T:${key}`;
  const fallbackKey = 'dataGovernance.standards.loadFailed';

  it('403/404 走本地化专用文案', () => {
    expect(loadErrorText({ status: 403 }, fallbackKey, translate)).toBe(
      'T:dataGovernance.standards.errorForbidden',
    );
    expect(loadErrorText({ status: 404 }, fallbackKey, translate)).toBe(
      'T:dataGovernance.standards.errorNotFound',
    );
  });

  it('其他 ApiError 有 message 时原样透出', () => {
    expect(
      loadErrorText({ status: 400, message: 'bad request' }, fallbackKey, translate),
    ).toBe('bad request');
  });

  it('没有可透出的后端 message 时回落通用 key', () => {
    expect(loadErrorText({ status: 500 }, fallbackKey, translate)).toBe(
      `T:${fallbackKey}`,
    );
    expect(loadErrorText(new Error('network down'), fallbackKey, translate)).toBe(
      `T:${fallbackKey}`,
    );
    expect(loadErrorText(null, fallbackKey, translate)).toBe(`T:${fallbackKey}`);
  });
});
