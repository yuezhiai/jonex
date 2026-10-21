/**
 * 监控读接口的错误分流（`pages/PlatformMonitor/errorText.ts`）。
 *
 * 4004 同时承载「跨度超限」与「起点早于保留期」两种含义，只靠业务码分不开，
 * 必须读 `errorDetails.reason`；后者还要把 `queryable_range` 原样带给用户。
 * 本文件用**普通对象**构造异常（不经过 axios）：分流函数只看 `status` / `bizCode` / `errorDetails`。
 */
import { describe, expect, it } from 'vitest';
import {
  classifyMonitorError,
  isNotFound,
  monitorErrorTextKeys,
  readRangeErrorDetails,
} from '../pages/PlatformMonitor/errorText';

/** 后端异常在请求层被补齐后的形状 */
function pmError(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { message: '请求失败', ...overrides };
}

describe('监控错误分流 · classifyMonitorError', () => {
  it('4004 + range_too_wide → rangeTooLong（跨度超上限）', () => {
    expect(
      classifyMonitorError(pmError({ bizCode: 4004, errorDetails: { reason: 'range_too_wide' } })),
    ).toBe('rangeTooLong');
  });

  it('4004 + out_of_retention → outOfRetention（起点早于保留期）', () => {
    expect(
      classifyMonitorError(pmError({ bizCode: 4004, errorDetails: { reason: 'out_of_retention' } })),
    ).toBe('outOfRetention');
  });

  it('4004 缺 reason 时按更宽泛的 rangeTooLong 处理', () => {
    expect(classifyMonitorError(pmError({ bizCode: 4004 }))).toBe('rangeTooLong');
    expect(classifyMonitorError(pmError({ bizCode: 4004, errorDetails: { reason: 'other' } }))).toBe(
      'rangeTooLong',
    );
  });

  it('4100 → unavailable，且优先于 HTTP 503 的通用上游判定', () => {
    expect(classifyMonitorError(pmError({ bizCode: 4100, status: 503 }))).toBe('unavailable');
  });

  it('3006 或 HTTP 403 → forbidden', () => {
    expect(classifyMonitorError(pmError({ bizCode: 3006, status: 403 }))).toBe('forbidden');
    expect(classifyMonitorError(pmError({ status: 403 }))).toBe('forbidden');
  });

  it('5001 或 HTTP 503 → upstream', () => {
    expect(classifyMonitorError(pmError({ bizCode: 5001, status: 503 }))).toBe('upstream');
    expect(classifyMonitorError(pmError({ status: 503 }))).toBe('upstream');
  });

  it('非对象 / 无码异常 → unknown', () => {
    expect(classifyMonitorError(null)).toBe('unknown');
    expect(classifyMonitorError(undefined)).toBe('unknown');
    expect(classifyMonitorError('boom')).toBe('unknown');
    expect(classifyMonitorError(new Error('boom'))).toBe('unknown');
    expect(classifyMonitorError(pmError({ status: 500 }))).toBe('unknown');
  });
});

describe('监控错误分流 · readRangeErrorDetails', () => {
  it('提取 reason / max_days / retention_days / queryable_range', () => {
    const error = pmError({
      bizCode: 4004,
      status: 409,
      errorDetails: {
        reason: 'out_of_retention',
        max_days: 30,
        retention_days: 90,
        queryable_range: { from: '2026-07-01T00:00:00+08:00', to: '2026-10-20T00:00:00+08:00' },
      },
    });

    expect(readRangeErrorDetails(error)).toEqual({
      reason: 'out_of_retention',
      maxDays: 30,
      retentionDays: 90,
      queryableRange: { from: '2026-07-01T00:00:00+08:00', to: '2026-10-20T00:00:00+08:00' },
    });
  });

  it('queryable_range 的端点缺失 / 非字符串时落 null（保留区间对象）', () => {
    const details = readRangeErrorDetails(
      pmError({
        bizCode: 4004,
        errorDetails: { reason: 'range_too_wide', queryable_range: { from: '2026-09-01T00:00:00Z' } },
      }),
    );

    expect(details.queryableRange).toEqual({ from: '2026-09-01T00:00:00Z', to: null });
  });

  it('字段类型不符时丢弃，不把字符串当数字', () => {
    const details = readRangeErrorDetails(
      pmError({
        bizCode: 4004,
        errorDetails: { reason: 42, max_days: '30', retention_days: null },
      }),
    );

    expect(details).toEqual({
      reason: undefined,
      maxDays: undefined,
      retentionDays: undefined,
      queryableRange: undefined,
    });
  });

  it('无 errorDetails 时返回空对象', () => {
    expect(readRangeErrorDetails(pmError({ bizCode: 4004 }))).toEqual({});
    expect(readRangeErrorDetails(null)).toEqual({});
    expect(readRangeErrorDetails('boom')).toEqual({});
  });
});

describe('监控错误分流 · 辅助判定', () => {
  it('isNotFound 只认 1002（确定性结果，不给重试）', () => {
    expect(isNotFound(pmError({ bizCode: 1002 }))).toBe(true);
    expect(isNotFound(pmError({ bizCode: 1001 }))).toBe(false);
    expect(isNotFound(null)).toBe(false);
  });

  it('monitorErrorTextKeys：known 档给标题与说明，unknown 档说明回落到后端 message', () => {
    expect(monitorErrorTextKeys('rangeTooLong')).toEqual({
      title: 'platformMonitor.error.rangeTooLong.title',
      description: 'platformMonitor.error.rangeTooLong.description',
    });
    expect(monitorErrorTextKeys('unknown')).toEqual({
      title: 'platformMonitor.error.unknown.title',
      description: null,
    });
  });
});
