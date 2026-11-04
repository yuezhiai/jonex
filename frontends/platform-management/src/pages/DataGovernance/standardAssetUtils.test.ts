import { describe, expect, it } from 'vitest';
import type { DgStandardAssetDetail } from './types';
import {
  isConflictError,
  isDraftDirty,
  kindLabelKey,
  normalizePayload,
  summarizeDraft,
  toStandardAssetItem,
  validateDraftFields,
} from './standardAssetUtils';

describe('kindLabelKey', () => {
  it('三类 kind 各自映射到 i18n 键', () => {
    expect(kindLabelKey('term')).toBe('dataGovernance.standardAssets.kind.term');
    expect(kindLabelKey('data_element')).toBe(
      'dataGovernance.standardAssets.kind.data_element',
    );
    expect(kindLabelKey('metric_card')).toBe(
      'dataGovernance.standardAssets.kind.metric_card',
    );
  });

  it('未登记的 kind 原样返回（兜底不抛错）', () => {
    expect(kindLabelKey('unknown_kind')).toBe('unknown_kind');
    expect(kindLabelKey('')).toBe('');
  });
});

describe('summarizeDraft', () => {
  it('草稿版本高于已发布版本 = 有草稿', () => {
    expect(summarizeDraft({ draftVersion: 2, publishedVersion: 1 })).toBe(true);
  });

  it('没有草稿版本 = 无草稿', () => {
    expect(summarizeDraft({ draftVersion: undefined, publishedVersion: 1 })).toBe(
      false,
    );
    expect(summarizeDraft({ draftVersion: null, publishedVersion: null })).toBe(
      false,
    );
  });

  it('从未发布过时，任意草稿版本都算有草稿', () => {
    expect(summarizeDraft({ draftVersion: 1, publishedVersion: null })).toBe(true);
    expect(summarizeDraft({ draftVersion: 1 })).toBe(true);
  });

  it('草稿版本与发布版本相同 = 无未发布变更', () => {
    expect(summarizeDraft({ draftVersion: 2, publishedVersion: 2 })).toBe(false);
  });
});

describe('isConflictError', () => {
  it('业务码 1003 或 HTTP 409 视为乐观锁冲突', () => {
    expect(isConflictError({ bizCode: 1003 })).toBe(true);
    expect(isConflictError({ status: 409 })).toBe(true);
    expect(isConflictError({ bizCode: 1003, status: 409 })).toBe(true);
  });

  it('其它错误码 / 非对象 / 字符串形态不算冲突', () => {
    expect(isConflictError({ bizCode: 1002 })).toBe(false);
    expect(isConflictError({ status: 500 })).toBe(false);
    expect(isConflictError(null)).toBe(false);
    expect(isConflictError('conflict')).toBe(false);
    // 只认运行时类型正确的字段，字符串 "409" 不误判。
    expect(isConflictError({ status: '409' })).toBe(false);
  });
});

describe('normalizePayload', () => {
  it('字符串 trim，空串丢弃', () => {
    expect(
      normalizePayload({ definition: '  术语定义  ', unit: '   ', formula: '' }),
    ).toEqual({ definition: '术语定义' });
  });

  it('数组只保留字符串元素并去空', () => {
    expect(
      normalizePayload({ aliases: [' 甲 ', '', '乙', 3 as unknown as string] }),
    ).toEqual({ aliases: ['甲', '乙'] });
    expect(normalizePayload({ aliases: ['   ', ''] })).toEqual({});
  });

  it('保留数字/布尔/嵌套对象，丢弃 null 与 undefined', () => {
    expect(
      normalizePayload({
        count: 0,
        enabled: false,
        nested: { a: 1 },
        empty: null,
        missing: undefined,
      }),
    ).toEqual({ count: 0, enabled: false, nested: { a: 1 } });
  });

  it('非对象入参按空 payload 处理', () => {
    expect(normalizePayload(null)).toEqual({});
    expect(normalizePayload(['a'])).toEqual({});
    expect(normalizePayload('x')).toEqual({});
  });
});

describe('isDraftDirty', () => {
  const detail = {
    name: '客户数',
    payload: { definition: '去重客户数', aliases: ['客户'], unit: '个' },
  };

  it('内容相同（仅空白差异）不算脏', () => {
    expect(isDraftDirty(detail, detail)).toBe(false);
    expect(
      isDraftDirty(detail, {
        name: ' 客户数 ',
        payload: {
          unit: ' 个 ',
          aliases: [' 客户 '],
          definition: '去重客户数 ',
        },
      }),
    ).toBe(false);
  });

  it('名称或 payload 变化算脏', () => {
    expect(isDraftDirty(detail, { ...detail, name: '客户数量' })).toBe(true);
    expect(
      isDraftDirty(detail, {
        ...detail,
        payload: { ...detail.payload, definition: '另一种定义' },
      }),
    ).toBe(true);
    expect(
      isDraftDirty(detail, {
        ...detail,
        payload: { ...detail.payload, aliases: ['客户', '顾客'] },
      }),
    ).toBe(true);
    // 清空已有字段也算脏（normalize 会丢掉该键）。
    expect(
      isDraftDirty(detail, {
        ...detail,
        payload: { definition: '去重客户数', aliases: ['客户'] },
      }),
    ).toBe(true);
  });
});

describe('toStandardAssetItem', () => {
  it('从详情去掉 payload，其余字段原样保留', () => {
    const detail: DgStandardAssetDetail = {
      assetId: 'sa_1',
      kind: 'term',
      code: 'customer',
      name: '客户',
      version: 2,
      status: 'draft',
      revision: 5,
      publishedVersion: 1,
      draftVersion: 2,
      hasDraft: true,
      updatedAt: '2026-10-19T00:00:00Z',
      updatedBy: 'u1',
      payload: { definition: 'x' },
    };
    const item = toStandardAssetItem(detail);
    expect(item).toEqual({
      assetId: 'sa_1',
      kind: 'term',
      code: 'customer',
      name: '客户',
      version: 2,
      status: 'draft',
      revision: 5,
      publishedVersion: 1,
      draftVersion: 2,
      hasDraft: true,
      updatedAt: '2026-10-19T00:00:00Z',
      updatedBy: 'u1',
    });
    expect('payload' in item).toBe(false);
  });
});

describe('validateDraftFields', () => {
  it('term 只要求 definition', () => {
    expect(validateDraftFields('term', { definition: 'x' })).toEqual([]);
    expect(validateDraftFields('term', {})).toEqual(['definition']);
  });

  it('metric_card 要求 definition / formula / unit', () => {
    const errors = validateDraftFields('metric_card', { definition: 'x' });
    expect(errors).toContain('formula');
    expect(errors).toContain('unit');
    expect(
      validateDraftFields('metric_card', {
        definition: 'x',
        formula: 'a/b',
        unit: '%',
      }),
    ).toEqual([]);
  });

  it('data_element 要求 definition 与 data_type', () => {
    expect(
      validateDraftFields('data_element', {
        definition: 'x',
        data_type: 'string',
      }),
    ).toEqual([]);
    expect(validateDraftFields('data_element', { definition: 'x' })).toEqual([
      'data_type',
    ]);
  });

  it('data_element.data_type 只接受五个枚举值', () => {
    for (const dataType of ['string', 'number', 'date', 'boolean', 'json']) {
      expect(
        validateDraftFields('data_element', { definition: 'x', data_type: dataType }),
      ).toEqual([]);
    }
    expect(
      validateDraftFields('data_element', { definition: 'x', data_type: 'float' }),
    ).toEqual(['data_type:enum']);
  });

  it('空白字符串与未知类型都按缺失处理', () => {
    expect(validateDraftFields('data_element', { definition: '   ' })).toEqual([
      'definition',
      'data_type',
    ]);
    expect(
      validateDraftFields('data_element', { definition: 'x', data_type: '   ' }),
    ).toEqual(['data_type']);
    expect(
      validateDraftFields('data_element', {
        definition: 'x',
        data_type: 42 as unknown as string,
      }),
    ).toEqual(['data_type']);
  });

  it('payload 为 null / 数组 / 标量时按空对象处理，必填项全部返回且不抛错', () => {
    expect(validateDraftFields('term', null)).toEqual(['definition']);
    expect(validateDraftFields('term', undefined)).toEqual(['definition']);
    expect(validateDraftFields('term', ['definition'])).toEqual(['definition']);
    expect(validateDraftFields('term', 'definition')).toEqual(['definition']);
    expect(validateDraftFields('data_element', 42)).toEqual([
      'definition',
      'data_type',
    ]);
    expect(validateDraftFields('metric_card', [])).toEqual([
      'definition',
      'formula',
      'unit',
    ]);
  });
});
