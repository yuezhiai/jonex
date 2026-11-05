import { describe, expect, it } from 'vitest';
import type { TFunction } from 'i18next';
import { humanizeSkipReason, relationEdgeOf, skipItemLabel } from './skipReasons';

/** 只回 key 的 t：本文件断言的是「映射到哪个文案键」。 */
const t = ((key: string) => key) as unknown as TFunction;

describe('relationEdgeOf', () => {
  it('两端齐全 → source.col → target.col', () => {
    expect(relationEdgeOf({
      source: 'Jobs', source_column: 'job_id',
      target: 'WorkingLifes', target_column: 'job_id',
    })).toBe('Jobs.job_id → WorkingLifes.job_id');
  });

  it('缺一端 → null（不伪造箭头）', () => {
    expect(relationEdgeOf({ source: 'Jobs', source_column: 'job_id' })).toBeNull();
    expect(relationEdgeOf({ target: 'Jobs', target_column: 'job_id' })).toBeNull();
  });
});

describe('humanizeSkipReason · 已知码', () => {
  it('tools_full → 工具覆盖上限文案（不再原样透出原始码）', () => {
    expect(humanizeSkipReason(t, { index: 0, why: 'tools_full' }, []))
      .toBe('evolutionConsole.skipWhyToolsFull');
  });

  it('未知码原样透出', () => {
    expect(humanizeSkipReason(t, { index: 0, why: 'some_raw_code' }, []))
      .toBe('some_raw_code');
  });

  it('synonym 跨组重复词 / 未知 op → 专属文案', () => {
    expect(humanizeSkipReason(t, {
      index: 0, why: 'cross_group_duplicate', term: '甲',
      terms: ['甲'], canonical: '乙',
    }, [])).toBe('evolutionConsole.skipWhySynonymCrossGroupDuplicate');
    expect(humanizeSkipReason(t, {
      index: 1, why: 'unknown_op', terms: ['甲'], canonical: '乙',
    }, [])).toBe('evolutionConsole.skipWhySynonymUnknownOp');
  });
});

describe('skipItemLabel · relation 跳过条目（形状可能残缺）', () => {
  it('只有 source + source_column', () => {
    expect(skipItemLabel({ index: 0, source: 'Jobs', source_column: 'job_id', why: 'x' }))
      .toBe('Jobs.job_id → ?');
  });

  it('只有 target + target_column', () => {
    expect(skipItemLabel({ index: 1, target: 'Users', target_column: 'user_id', why: 'x' }))
      .toBe('? → Users.user_id');
  });

  it('只有 name', () => {
    expect(skipItemLabel({ index: 2, name: 'REL_JOBS_JOB_ID_USERS_USER_ID', why: 'x' }))
      .toBe('REL_JOBS_JOB_ID_USERS_USER_ID');
  });

  it('只有 index（非 dict / 超上限）→ #下标，不渲染空白', () => {
    expect(skipItemLabel({ index: 3, why: '条目必须是对象' })).toBe('#3');
  });

  it('两端齐全优先于 name', () => {
    expect(skipItemLabel({
      index: 4, name: 'REL_X',
      source: 'Jobs', source_column: 'job_id',
      target: 'Users', target_column: 'user_id',
      why: 'x',
    })).toBe('Jobs.job_id → Users.user_id');
  });

  it('列名缺失时用 ? 占位', () => {
    expect(skipItemLabel({ index: 5, source: 'Jobs', why: 'x' })).toBe('Jobs.? → ?');
  });

  it('alias / query_rule 形状保持原样', () => {
    expect(skipItemLabel({ concept: '人员', alias: '工作', why: 'x' })).toBe('人员 ＋ 「工作」');
    expect(skipItemLabel({ target_code: 'Orders.amount', why: 'x' })).toBe('Orders.amount');
  });
});
