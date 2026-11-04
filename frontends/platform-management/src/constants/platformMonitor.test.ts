import { describe, expect, it } from 'vitest';
import { pmFailureDrillParams, pmObjectCallsDrillParams } from './platformMonitor';

describe('pmFailureDrillParams', () => {
  it('外部依赖失败下钻：outbound + protocol=openai，与 KPI 的 external 口径对齐', () => {
    const next = pmFailureDrillParams('target', new URLSearchParams('tab=call&sub=external&win=24h'));

    expect(next.get('sub')).toBe('records');
    expect(next.get('status')).toBe('failed');
    expect(next.get('direction')).toBe('outbound');
    // 当前 call_scope=external ≡ outbound + openai；C6 没有 call_scope，只能显式补协议
    expect(next.get('protocol')).toBe('openai');
    expect(next.get('win')).toBe('24h');
  });

  it('开放能力失败下钻：只筛 inbound，不补协议', () => {
    const next = pmFailureDrillParams('capability', new URLSearchParams('tab=call&sub=capability'));

    expect(next.get('direction')).toBe('inbound');
    expect(next.has('protocol')).toBe(false);
  });

  it('下钻前清掉记录页残留的其它筛选', () => {
    const next = pmFailureDrillParams(
      'target',
      new URLSearchParams('sub=records&status=success&trigger=system&source=user:1&target=old&keyword=x'),
    );

    expect(next.get('status')).toBe('failed');
    expect(next.has('trigger')).toBe(false);
    expect(next.has('source')).toBe(false);
    expect(next.has('target')).toBe(false);
    expect(next.has('keyword')).toBe(false);
  });
});

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
