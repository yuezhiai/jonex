import { describe, expect, it } from 'vitest';
import { pmObjectCallsDrillParams } from './platformMonitor';

describe('pmObjectCallsDrillParams', () => {
  it('切到记录页并精确带上对象标识，保留时间窗', () => {
    const current = new URLSearchParams(
      'tab=call&sub=external&win=24h&status=failed&direction=outbound&target=old&protocol=openai',
    );

    const next = pmObjectCallsDrillParams('model.qwen', null, current);

    expect(next.get('tab')).toBe('call');
    expect(next.get('sub')).toBe('records');
    expect(next.get('win')).toBe('24h');
    expect(next.get('target')).toBe('model.qwen');
    // 上一次下钻留下的筛选不属于这次跳转
    expect(next.has('status')).toBe(false);
    expect(next.has('direction')).toBe(false);
    expect(next.has('protocol')).toBe(false);
  });

  it('开放能力侧带上点进来那一行的协议，保证与对象详情同口径', () => {
    const next = pmObjectCallsDrillParams(
      'business.knowledge_base.v1',
      'invoke',
      new URLSearchParams('tab=call&sub=capability'),
    );

    expect(next.get('target')).toBe('business.knowledge_base.v1');
    expect(next.get('protocol')).toBe('invoke');
  });

  it('不修改调用方传入的 URLSearchParams', () => {
    const current = new URLSearchParams('tab=call&sub=external');

    pmObjectCallsDrillParams('svc-a', null, current);

    expect(current.get('sub')).toBe('external');
    expect(current.has('target')).toBe(false);
  });
});
