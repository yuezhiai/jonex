import { describe, expect, it } from 'vitest';
import { actionsFor, STATUS_TAGS } from './forbiddenModel';

describe('forbiddenModel', () => {
  it('proposed 可采纳/忽略/删除', () => {
    expect(actionsFor('proposed')).toEqual(['adopt', 'disable', 'delete']);
  });
  it('active 可停用/删除', () => {
    expect(actionsFor('active')).toEqual(['disable', 'delete']);
  });
  it('disabled 可启用/删除', () => {
    expect(actionsFor('disabled')).toEqual(['enable', 'delete']);
  });
  it('未知状态不崩、不给动作', () => {
    expect(actionsFor('weird')).toEqual([]);
    expect(STATUS_TAGS.weird).toBeUndefined();
  });
});
