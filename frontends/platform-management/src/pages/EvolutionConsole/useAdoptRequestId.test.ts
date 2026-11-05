import { describe, expect, it } from 'vitest';
import {
  adoptRequestKey,
  createAdoptRequestIdHolder,
} from './useAdoptRequestId';

describe('adoptRequestKey', () => {
  it('勾选顺序不影响 key，但集合变化会变 key', () => {
    expect(adoptRequestKey(['b', 'a'], '', []))
      .toBe(adoptRequestKey(['a', 'b'], '', []));
    expect(adoptRequestKey(['a'], '', []))
      .not.toBe(adoptRequestKey(['a', 'b'], '', []));
  });

  it('override 理由（去首尾空白）与 truth ids 都参与 key', () => {
    expect(adoptRequestKey([], ' 原因 ', [])).toBe(adoptRequestKey([], '原因', []));
    expect(adoptRequestKey([], '', ['c1']))
      .not.toBe(adoptRequestKey([], '', ['c2']));
  });
});

describe('createAdoptRequestIdHolder', () => {
  it('同一 key 的重试复用同一个 request_id', () => {
    const holder = createAdoptRequestIdHolder();
    const first = holder.ensure('same');
    const second = holder.ensure('same');

    expect(first.startsWith('adopt_')).toBe(true);
    expect(second).toBe(first);
    expect(holder.peek('same')).toBe(first);
  });

  it('key 变化后丢弃旧 id，下次确认重新生成', () => {
    const holder = createAdoptRequestIdHolder();
    const first = holder.ensure('a');
    const second = holder.ensure('b');

    expect(second).not.toBe(first);
    expect(holder.peek('a')).toBeNull();
    expect(holder.peek('b')).toBe(second);
  });

  it('reset 后即使是同一 key 也会重新生成', () => {
    const holder = createAdoptRequestIdHolder();
    const first = holder.ensure('a');
    holder.reset();
    const second = holder.ensure('a');

    expect(second).not.toBe(first);
  });
});
