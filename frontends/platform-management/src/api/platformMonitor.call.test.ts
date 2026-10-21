/**
 * 调用监控取数层（统计卡 / 对象列表 / 趋势 / 记录 / 详情 / attempt）—— 契约与映射验收。
 *
 * 与 `platformMonitor.task.test.ts` 同一套手法：用**真实的 axios 实例**，只把 adapter（传输层）换掉，
 * 「网线之外」的东西（拦截器、信封校验、`unwrap` 解包）全走生产代码。断言钉五件事：
 *   ① **路径与参数**：各读接口打契约路径，空筛选不下发该 key、分页有默认值；
 *   ② **null 语义**：空窗口的 `success_rate` / 趋势点 `value` / `avg_latency_ms` 保持 null，不得兜 0；
 *   ③ **量纲**：比率 ×100；**调用耗时保持毫秒**（与任务族的秒不同）；
 *   ④ **口径元数据**：对象列表 / 趋势 / 记录接口的 `mode_meta` 与详情接口的内联 `mode_meta`
 *      都映射为 `metricMode`；
 *   ⑤ **时区族**：调用监控请求发 UTC ISO（`Z`），响应里的 `Z` 刻度按平台时区（+08:00）出字。
 *
 * 边界：本文件只测「接口 JSON → types.ts」这一层，不渲染 React（页面状态属组件测试的范围）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import {
  getAllCallScopeObjects,
  getCallAttempts,
  getCallDetail,
  getCallObjectDetail,
  getCallRecords,
  getCallStats,
  getCallTargets,
  getCallTrend,
  getCapabilityStats,
  getOpenCapabilities,
} from './platformMonitor';

const ENDPOINT_EXTERNAL_SUMMARY = '/platform/monitor/calls/external/summary';
const ENDPOINT_OPEN_SUMMARY = '/platform/monitor/calls/open/summary';
const ENDPOINT_EXTERNAL_OBJECTS = '/platform/monitor/calls/external/objects';
const ENDPOINT_OPEN_OBJECTS = '/platform/monitor/calls/open/objects';
const ENDPOINT_TREND = '/platform/monitor/calls/trend';
const ENDPOINT_RECORDS = '/platform/monitor/calls/records';
const ENDPOINT_DETAIL = '/platform/monitor/calls/req-1/detail';
const ENDPOINT_ATTEMPTS = '/platform/monitor/calls/req-1/attempts';
const ENDPOINT_OBJECT_DETAIL = '/platform/monitor/calls/objects/model.qwen/detail';

/** 内存版 localStorage：请求拦截器要读 locale，node 环境没有原生实现 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
  vi.stubGlobal('localStorage', store);
  // ⚠ 必须同时注入 window：shell-lib 的 getStorage() 以 typeof window === 'undefined' 判空
  vi.stubGlobal('window', { localStorage: store });
}

function makeResponse(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { data, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse;
}

type AdapterSlot = { adapter: AxiosAdapter | undefined };
const adapterSlot = (): AdapterSlot => apiClient.raw.defaults as unknown as AdapterSlot;

/** 一次请求的落点：url 与 params（断言入参用） */
interface Hit {
  url: string;
  params: Record<string, unknown>;
}

let hits: Hit[] = [];
/** url → 业务 data 的桩；缺省即未桩化（会抛，免得静默打到别处） */
let routes: Record<string, () => unknown> = {};

function stub(): void {
  const adapter: AxiosAdapter = async (config) => {
    const url = String(config.url ?? '');
    hits.push({ url, params: (config.params ?? {}) as Record<string, unknown> });
    const handler = routes[url];
    if (!handler) throw new Error(`未桩化的请求：${url}`);
    return makeResponse(config, { success: true, code: 0, data: handler() });
  };
  adapterSlot().adapter = adapter;
}

const originalAdapter = apiClient.raw.defaults.adapter;

/** 统计卡的最小 DTO，各用例只覆盖关心的字段 */
function summaryDto(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    total_calls: 0,
    success_rate: null,
    avg_latency_ms: null,
    failed_count: 0,
    ...overrides,
  };
}

/** 对象列表的一行 */
function objectDto(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    object_id: 'model.qwen',
    object_type: 'openai',
    protocol: 'openai',
    call_count: 10,
    success_rate: 0.96,
    avg_latency_ms: 120,
    failed_count: 1,
    last_called_at: '2026-10-13T08:40:00Z',
    ...overrides,
  };
}

/** 调用记录的一行，字段取全，便于各用例只覆盖关心的那几个 */
function recordDto(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    request_id: 'req-1',
    attempt_id: null,
    direction: 'outbound',
    trigger_type: 'user',
    caller: 'task-center',
    target: 'model.qwen',
    protocol: 'openai',
    status: 'success',
    error_code: null,
    duration_ms: 1426,
    logical_started_at: '2026-10-13T08:40:00+00:00',
    ...overrides,
  };
}

const baseQuery = {
  direction: 'all' as const,
  status: 'all' as const,
  triggerType: 'all' as const,
  protocol: 'all' as const,
  source: '',
  target: '',
  keyword: '',
  page: 1,
  pageSize: 20,
};

describe('调用监控取数层契约与映射', () => {
  beforeEach(() => {
    installLocalStorage();
    hits = [];
    routes = {};
    stub();
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  describe('外部依赖统计卡', () => {
    it('打契约路径，时间参数为 UTC ISO 的 from/to + range_preset', async () => {
      routes[ENDPOINT_EXTERNAL_SUMMARY] = () => summaryDto();

      await getCallStats('24h');

      expect(hits).toHaveLength(1);
      expect(hits[0].url).toBe(ENDPOINT_EXTERNAL_SUMMARY);
      expect(hits[0].params.range_preset).toBe('24h');
      // 调用监控接口的 from/to 是 UTC ISO（`Z` 结尾），与任务族的 +08:00 不同
      expect(hits[0].params.from).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
      expect(hits[0].params.to).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
      const spanHours =
        (Date.parse(hits[0].params.to as string) - Date.parse(hits[0].params.from as string)) / 3600_000;
      expect(Math.round(spanHours)).toBe(24);
    });

    it('比率 ×100、耗时保持毫秒、moduleError 与 metricMode 落位', async () => {
      routes[ENDPOINT_EXTERNAL_SUMMARY] = () =>
        summaryDto({
          total_calls: 100,
          success_rate: 0.96,
          avg_latency_ms: 120,
          failed_count: 4,
          mode_meta: { metric_mode: 'degraded' },
          flags: { module_error: '部分采集链路不可用' },
          as_of: '2026-10-13T08:00:00+00:00',
        });

      const stats = await getCallStats('24h');

      expect(stats.successRate).toBeCloseTo(96, 6);
      // 调用延迟本身就是毫秒，不得 ÷1000
      expect(stats.avgLatencyMs).toBe(120);
      expect(stats.moduleError).toBe('部分采集链路不可用');
      expect(stats.metricMode).toBe('degraded');
      expect(stats.asOf).toBe('2026-10-13T08:00:00+00:00');
    });

    it('空窗口：success_rate / avg_latency_ms / flags 缺失时保持 null（禁兜 0）', async () => {
      routes[ENDPOINT_EXTERNAL_SUMMARY] = () => summaryDto();

      const stats = await getCallStats('24h');

      expect(stats.successRate).toBeNull();
      expect(stats.avgLatencyMs).toBeNull();
      expect(stats.moduleError).toBeNull();
      expect(stats.metricMode).toBeNull();
      expect(stats.asOf).toBe('');
    });

    it('mode_meta 只认 logical / degraded，其他取值归 null', async () => {
      const withMode = (mode: unknown) => () => summaryDto({ mode_meta: { metric_mode: mode } });

      routes[ENDPOINT_EXTERNAL_SUMMARY] = withMode('logical');
      expect((await getCallStats('24h')).metricMode).toBe('logical');

      routes[ENDPOINT_EXTERNAL_SUMMARY] = withMode('unknown-mode');
      expect((await getCallStats('24h')).metricMode).toBeNull();
    });
  });

  describe('开放能力统计卡', () => {
    it('打 open/summary，参数与映射同外部依赖统计卡', async () => {
      routes[ENDPOINT_OPEN_SUMMARY] = () =>
        summaryDto({
          total_calls: 7,
          success_rate: 0.5,
          avg_latency_ms: 880,
          failed_count: 3,
          flags: { module_error: null },
        });

      const stats = await getCapabilityStats('7d');

      expect(hits[0].url).toBe(ENDPOINT_OPEN_SUMMARY);
      expect(hits[0].params.range_preset).toBe('7d');
      expect(stats.total).toBe(7);
      expect(stats.successRate).toBeCloseTo(50, 6);
      expect(stats.avgLatencyMs).toBe(880);
      expect(stats.moduleError).toBeNull();
      expect(stats.metricMode).toBeNull();
    });
  });

  describe('外部依赖对象列表', () => {
    it('默认分页 page=1 / page_size=20，mode_meta 映射为 metricMode', async () => {
      routes[ENDPOINT_EXTERNAL_OBJECTS] = () => ({
        items: [objectDto()],
        total: 1,
        mode_meta: { metric_mode: 'degraded' },
      });

      const page = await getCallTargets('24h');

      expect(hits[0].url).toBe(ENDPOINT_EXTERNAL_OBJECTS);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(20);
      expect(page.total).toBe(1);
      expect(page.metricMode).toBe('degraded');
    });

    it('行映射：名字取 object_id、比率 ×100、null 原样透传', async () => {
      routes[ENDPOINT_EXTERNAL_OBJECTS] = () => ({
        items: [
          objectDto(),
          objectDto({
            object_id: 'business.knowledge_base.v1',
            object_type: 'invoke',
            protocol: 'invoke',
            success_rate: null,
            avg_latency_ms: null,
          }),
        ],
        total: 2,
      });

      const page = await getCallTargets('24h', 2, 50);

      expect(hits[0].params.page).toBe(2);
      expect(hits[0].params.page_size).toBe(50);
      expect(page.items[0].name).toBe('model.qwen');
      expect(page.items[0].successRate).toBeCloseTo(96, 6);
      expect(page.items[0].avgLatencyMs).toBe(120);
      // 空窗口 = 没有调用，不是「全部失败」⇒ 不得兜 0
      expect(page.items[1].successRate).toBeNull();
      expect(page.items[1].avgLatencyMs).toBeNull();
      // 时间按平台时区出字（UTC 08:40 → 北京 16:40）
      expect(page.items[0].lastCallAt).toBe('10-13 16:40:00');
    });

    it('metric_mode 缺失 / 未知时 metricMode 为 null', async () => {
      routes[ENDPOINT_EXTERNAL_OBJECTS] = () => ({ items: [], total: 0 });

      expect((await getCallTargets('24h')).metricMode).toBeNull();

      routes[ENDPOINT_EXTERNAL_OBJECTS] = () => ({
        items: [],
        total: 0,
        mode_meta: { metric_mode: 'bogus' },
      });
      expect((await getCallTargets('24h')).metricMode).toBeNull();
    });

    it('全量清单按 page_size=100 逐页拉全，与明细表分页解耦', async () => {
      let round = 0;
      routes[ENDPOINT_EXTERNAL_OBJECTS] = () => {
        round += 1;
        return round === 1
          ? {
              items: Array.from({ length: 100 }, (_, i) => objectDto({ object_id: `obj-${i}` })),
              total: 120,
              mode_meta: { metric_mode: 'degraded' },
            }
          : {
              items: Array.from({ length: 20 }, (_, i) => objectDto({ object_id: `obj-${100 + i}` })),
              total: 120,
            };
      };

      const page = await getAllCallScopeObjects('24h', 'target');

      expect(page.items).toHaveLength(120);
      expect(page.total).toBe(120);
      // 口径取第一页，整次清单统一
      expect(page.metricMode).toBe('degraded');
      expect(hits.map((hit) => hit.params.page)).toEqual([1, 2]);
      expect(hits.every((hit) => hit.params.page_size === 100)).toBe(true);
      for (const hit of hits) {
        for (const key of ['object_type', 'protocol']) expect(key in hit.params).toBe(false);
      }
    });
  });

  describe('开放能力列表', () => {
    it('默认分页 + 不传 protocol 时不下发该 key；mode_meta 映射', async () => {
      routes[ENDPOINT_OPEN_OBJECTS] = () => ({
        items: [],
        total: 0,
        mode_meta: { metric_mode: 'logical' },
      });

      const page = await getOpenCapabilities('24h');

      expect(hits[0].url).toBe(ENDPOINT_OPEN_OBJECTS);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(20);
      expect('protocol' in hits[0].params).toBe(false);
      expect(page.metricMode).toBe('logical');
    });

    it('传 protocol 时原样下发同名参数', async () => {
      routes[ENDPOINT_OPEN_OBJECTS] = () => ({ items: [objectDto()], total: 1 });

      const page = await getOpenCapabilities('24h', 3, 10, 'invoke');

      expect(hits[0].params.page).toBe(3);
      expect(hits[0].params.page_size).toBe(10);
      expect(hits[0].params.protocol).toBe('invoke');
      expect(page.items[0].name).toBe('model.qwen');
    });
  });

  describe('调用趋势', () => {
    it('direction 原样进 params，两个页签据此分线', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getCallTrend('24h', 'volume', 'outbound');
      await getCallTrend('24h', 'volume', 'inbound');

      expect(hits[0].url).toBe(ENDPOINT_TREND);
      expect(hits[0].params.direction).toBe('outbound');
      expect(hits[1].params.direction).toBe('inbound');
    });

    it('不传 direction / objectId=all 时都不下发对应 key', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getCallTrend('24h', 'volume', undefined, 'all');

      expect('direction' in hits[0].params).toBe(false);
      expect('object_id' in hits[0].params).toBe(false);
    });

    it('metric 改名 + bucket 随窗口；返回 {points, metricMode}', async () => {
      routes[ENDPOINT_TREND] = () => ({
        points: [{ bucket_start: '2026-10-13T00:00:00Z', value: 1426, closed: true }],
        mode_meta: { metric_mode: 'degraded' },
      });

      const trend = await getCallTrend('24h', 'avgDuration', 'outbound');

      expect(hits[0].params.metric).toBe('avg_latency');
      expect(hits[0].params.bucket).toBe('hour');
      // 耗时点保持毫秒：调用延迟普遍在百毫秒级，换秒会全变 0
      expect(trend.points[0].value).toBe(1426);
      expect(trend.metricMode).toBe('degraded');

      routes[ENDPOINT_TREND] = () => ({ points: [] });
      await getCallTrend('7d', 'successRate', 'outbound');
      expect(hits[1].params.metric).toBe('success_rate');
      expect(hits[1].params.bucket).toBe('day');
    });

    it('四档指标逐一对上契约取值', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getCallTrend('24h', 'volume', 'outbound');
      await getCallTrend('24h', 'successRate', 'outbound');
      await getCallTrend('24h', 'avgDuration', 'outbound');
      await getCallTrend('24h', 'failures', 'outbound');

      expect(hits.map((h) => h.params.metric)).toEqual([
        'call_count',
        'success_rate',
        'avg_latency',
        'failed_count',
      ]);
    });

    it('成功率点 ×100、空桶 null 透传；UTC 刻度按平台时区出字', async () => {
      routes[ENDPOINT_TREND] = () => ({
        points: [
          { bucket_start: '2026-10-13T00:00:00Z', value: 0.96, closed: true },
          { bucket_start: '2026-10-13T01:00:00Z', value: null, closed: false },
        ],
      });

      const trend = await getCallTrend('24h', 'successRate', 'inbound');

      expect(trend.points[0].value).toBeCloseTo(96, 6);
      expect(trend.points[1].value).toBeNull();
      // UTC 00:00 = 北京 08:00；不能直接按浏览器本地时区格式化
      expect(trend.points[0].date).toBe('08:00');
    });

    it('mode_meta 缺失 / 未知时 metricMode 为 null', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [{ bucket_start: '2026-10-13T00:00:00Z', value: 1, closed: true }] });

      expect((await getCallTrend('24h', 'volume', 'outbound')).metricMode).toBeNull();

      routes[ENDPOINT_TREND] = () => ({
        points: [],
        mode_meta: { metric_mode: 'bogus' },
      });
      expect((await getCallTrend('24h', 'volume', 'outbound')).metricMode).toBeNull();
    });
  });

  describe('调用记录', () => {
    it('筛选项与分页下发；「全部」与空串不下发对应 key', async () => {
      routes[ENDPOINT_RECORDS] = () => ({ items: [], total: 0 });

      await getCallRecords('24h', baseQuery);

      expect(hits[0].url).toBe(ENDPOINT_RECORDS);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(20);
      for (const key of ['direction', 'status', 'trigger_type', 'protocol', 'source', 'target', 'keyword']) {
        expect(key in hits[0].params).toBe(false);
      }
    });

    it('有效筛选原样下发同名接口参数', async () => {
      routes[ENDPOINT_RECORDS] = () => ({ items: [], total: 0 });

      await getCallRecords('7d', {
        ...baseQuery,
        direction: 'inbound',
        status: 'failed',
        triggerType: 'system',
        protocol: 'mcp',
        source: 'task-center',
        target: 'model.qwen',
        keyword: 'timeout',
        page: 2,
        pageSize: 5,
      });

      expect(hits[0].params).toMatchObject({
        direction: 'inbound',
        status: 'failed',
        trigger_type: 'system',
        protocol: 'mcp',
        source: 'task-center',
        // 目标对象走精确匹配的独立参数，不与 keyword 混用
        target: 'model.qwen',
        keyword: 'timeout',
        page: 2,
        page_size: 5,
      });
    });

    it('降级模式：attempt_id 映射进 attemptId，同 requestId 的多行仍可区分', async () => {
      routes[ENDPOINT_RECORDS] = () => ({
        items: [
          recordDto({ attempt_id: 'att-1', status: 'failed' }),
          recordDto({ attempt_id: 'att-2', status: 'success' }),
        ],
        total: 2,
        mode_meta: { metric_mode: 'degraded' },
      });

      const page = await getCallRecords('24h', baseQuery);

      expect(page.items[0].attemptId).toBe('att-1');
      expect(page.items[1].attemptId).toBe('att-2');
      expect(page.items[0].requestId).toBe(page.items[1].requestId);
      expect(page.metricMode).toBe('degraded');
    });

    it('正常模式 attemptId=null；耗时保持毫秒、时间按平台时区', async () => {
      routes[ENDPOINT_RECORDS] = () => ({ items: [recordDto()], total: 1 });

      const page = await getCallRecords('24h', baseQuery);

      expect(page.items[0].attemptId).toBeNull();
      expect(page.items[0].latencyMs).toBe(1426);
      expect(page.items[0].time).toBe('10-13 16:40:00');
      expect(page.items[0].traceId).toBeNull();
      expect(page.metricMode).toBeNull();
    });
  });

  describe('单次调用详情', () => {
    it('带 attemptId 时下发 attempt_id；内联 mode_meta 映射为 metricMode', async () => {
      routes[ENDPOINT_DETAIL] = () => ({
        request_id: 'req-1',
        method: 'POST',
        path: '/v1/chat/completions',
        status_code: 500,
        error_message: '上游超时',
        request_payload: null,
        response_payload: null,
        prompt_tokens: null,
        completion_tokens: null,
        total_tokens: null,
        stage: 2,
        meta: null,
        mode_meta: { metric_mode: 'degraded' },
      });

      const detail = await getCallDetail('req-1', 'att-2');

      expect(hits[0].url).toBe(ENDPOINT_DETAIL);
      expect(hits[0].params.attempt_id).toBe('att-2');
      expect(detail.statusCode).toBe(500);
      expect(detail.meta).toBeNull();
      expect(detail.metricMode).toBe('degraded');
    });

    it('不带 attemptId 时不下发该 key（正常模式 requestId 自身唯一）', async () => {
      routes[ENDPOINT_DETAIL] = () => ({
        request_id: 'req-1',
        mode_meta: { metric_mode: 'logical' },
      });

      const detail = await getCallDetail('req-1');

      expect('attempt_id' in hits[0].params).toBe(false);
      expect(detail.metricMode).toBe('logical');
    });
  });

  describe('attempt 明细', () => {
    it('默认分页 page=1 / page_size=20，retry_count 映射为 retryCount', async () => {
      routes[ENDPOINT_ATTEMPTS] = () => ({
        items: [
          {
            attempt_id: 'att-1',
            attempt_no: 1,
            attempt_started_at: '2026-10-13T08:40:00.120Z',
            attempt_ended_at: '2026-10-13T08:40:01.546Z',
            duration_ms: 1426,
            status: 'failed',
            error_code: 'timeout',
            error_message: '上游超时',
            is_final: false,
            wait_before_ms: 0,
          },
        ],
        total: 2,
        retry_count: 1,
      });

      const page = await getCallAttempts('req-1');

      expect(hits[0].url).toBe(ENDPOINT_ATTEMPTS);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(20);
      expect(page.total).toBe(2);
      expect(page.retryCount).toBe(1);
      expect(page.items[0].durationMs).toBe(1426);
      // attempt 之间常只差几百毫秒，格式必须带毫秒（UTC 08:40:00.120 → 北京 16:40:00.120）
      expect(page.items[0].attemptStartedAt).toBe('10-13 16:40:00.120');
    });
  });

  describe('对象 / 能力详情', () => {
    it('打 objects/{id}/detail；不传 protocol 时不下发该 key', async () => {
      routes[ENDPOINT_OBJECT_DETAIL] = () => ({
        object_id: 'model.qwen',
        object_type: 'openai',
        call_count: 100,
        success_rate: 0.962,
        avg_latency_ms: 120,
        failed_count: 4,
        top_sources: [{ name: 'task-center', call_count: 99 }],
        recent_failures: [
          { request_id: 'req-1', error_message: '上游超时', logical_started_at: '2026-10-13T08:40:00Z' },
        ],
        mode_meta: { metric_mode: 'logical' },
      });

      const detail = await getCallObjectDetail('model.qwen', '24h');

      expect(hits[0].url).toBe(ENDPOINT_OBJECT_DETAIL);
      expect('protocol' in hits[0].params).toBe(false);
      // 比率 ×100，延迟保持毫秒
      expect(detail.successRate).toBeCloseTo(96.2, 6);
      expect(detail.avgLatencyMs).toBe(120);
      expect(detail.topSources).toEqual([{ name: 'task-center', callCount: 99 }]);
      expect(detail.recentFailures[0].time).toBe('10-13 16:40:00');
      expect(detail.metricMode).toBe('logical');
    });

    it('传 protocol 时下发；空窗口 success_rate / avg_latency_ms 保持 null', async () => {
      routes[ENDPOINT_OBJECT_DETAIL] = () => ({
        object_id: 'model.qwen',
        object_type: 'openai',
        call_count: 0,
        success_rate: null,
        avg_latency_ms: null,
        failed_count: 0,
        top_sources: [],
        recent_failures: [],
      });

      const detail = await getCallObjectDetail('model.qwen', '24h', 'openai');

      expect(hits[0].params.protocol).toBe('openai');
      expect(detail.successRate).toBeNull();
      expect(detail.avgLatencyMs).toBeNull();
      expect(detail.metricMode).toBeNull();
    });
  });
});
