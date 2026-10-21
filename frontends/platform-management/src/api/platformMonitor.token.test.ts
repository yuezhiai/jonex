/**
 * Token 监控取数层（统计卡 / 趋势 / 模型分布 / 模型用量）—— 契约与映射验收。
 *
 * 与 `platformMonitor.task.test.ts` 同一套手法（真实 axios 实例 + 换掉 adapter），断言钉：
 *   ① **路径与入参**：统计卡只打当前窗口一次（本期不做环比）；趋势 / 分布 / 用量接口的
 *      metric/bucket/keyword/分页/排序照契约拼装；
 *   ② **映射**：`total_tokens / prompt_tokens / completion_tokens` 三种口径 → 趋势点，
 *      `completeness_rate` ×100；
 *   ③ **null 透传**：空窗口的均值、完整率一律 `null`，不得兜成 0；
 *   ④ **时区族**：Token 监控与调用监控一致，请求发 UTC ISO、响应刻度按平台时区出字。
 *
 * 边界：只测「接口 JSON → types.ts」这一层，不渲染 React。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import { getModelShare, getModelUsage, getTokenStats, getTokenTrend } from './platformMonitor';

const ENDPOINT_SUMMARY = '/platform/monitor/tokens/summary';
const ENDPOINT_TREND = '/platform/monitor/tokens/trend';
const ENDPOINT_TOP = '/platform/monitor/tokens/models/top';
const ENDPOINT_USAGE = '/platform/monitor/tokens/usage';

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
  vi.stubGlobal('window', { localStorage: store });
}

function makeResponse(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { data, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse;
}

type AdapterSlot = { adapter: AxiosAdapter | undefined };
const adapterSlot = (): AdapterSlot => apiClient.raw.defaults as unknown as AdapterSlot;

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

const summaryDto = (overrides: Record<string, unknown> = {}) => ({
  call_count: 0,
  total_tokens: 0,
  avg_tokens_per_call: null,
  model_count: 0,
  completeness_rate: null,
  flags: { data_delayed: false, demo_data: false, module_error: null },
  as_of: '',
  ...overrides,
});

describe('Token 监控取数层契约与映射', () => {
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

  describe('统计卡', () => {
    it('只打一次当前窗口，range_preset 回显档位；from/to 为 UTC ISO', async () => {
      routes[ENDPOINT_SUMMARY] = () =>
        summaryDto({
          call_count: 150,
          total_tokens: 3000,
          avg_tokens_per_call: 200,
          model_count: 4,
          as_of: '2026-10-13T08:00:00+00:00',
        });

      const stats = await getTokenStats('24h');

      expect(hits).toHaveLength(1);
      expect(hits[0].url).toBe(ENDPOINT_SUMMARY);
      expect(hits[0].params.range_preset).toBe('24h');
      expect(hits[0].params.from).toMatch(/Z$/);
      expect(hits[0].params.to).toMatch(/Z$/);
      expect(stats.modelCalls).toBe(150);
      expect(stats.totalTokens).toBe(3000);
      expect(stats.avgTokensPerCall).toBe(200);
      expect(stats.modelCount).toBe(4);
      expect(stats.asOf).toBe('2026-10-13T08:00:00+00:00');
    });

    it('completeness_rate ×100；空窗口的完整率 / 均值保持 null（禁兜 0）', async () => {
      routes[ENDPOINT_SUMMARY] = () => summaryDto({ completeness_rate: 0.98 });
      expect((await getTokenStats('24h')).completenessRate).toBeCloseTo(98, 6);

      routes[ENDPOINT_SUMMARY] = () => summaryDto();
      const empty = await getTokenStats('24h');
      expect(empty.completenessRate).toBeNull();
      expect(empty.avgTokensPerCall).toBeNull();
    });

    it('flags.module_error 落到 moduleError，供页面与「没数据」区分', async () => {
      routes[ENDPOINT_SUMMARY] = () =>
        summaryDto({ flags: { data_delayed: false, demo_data: false, module_error: '本模块暂不可用' } });

      expect((await getTokenStats('24h')).moduleError).toBe('本模块暂不可用');

      routes[ENDPOINT_SUMMARY] = () => summaryDto({ flags: undefined });
      expect((await getTokenStats('24h')).moduleError).toBeNull();
    });
  });

  describe('时间窗档位', () => {
    it('30d 按 30 天回推，趋势用 day 桶', async () => {
      routes[ENDPOINT_SUMMARY] = () => summaryDto();
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTokenStats('30d');
      await getTokenTrend('30d', 'total');

      const days =
        (Date.parse(hits[0].params.to as string) - Date.parse(hits[0].params.from as string)) / 86400_000;
      expect(Math.round(days)).toBe(30);
      expect(hits[1].params.bucket).toBe('day');
    });

    it('自定义区间原样下发 from/to，跨度超过 48h 用 day 桶', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });
      const from = '2026-09-01T00:00:00.000Z';
      const to = '2026-09-10T00:00:00.000Z';

      await getTokenTrend({ preset: 'custom', from, to }, 'input');

      expect(hits[0].params.from).toBe(from);
      expect(hits[0].params.to).toBe(to);
      expect(hits[0].params.range_preset).toBe('custom');
      expect(hits[0].params.bucket).toBe('day');
    });
  });

  describe('趋势', () => {
    it('24h 用 bucket=hour，7d 用 bucket=day；metric 直接取口径名', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTokenTrend('24h', 'input');
      await getTokenTrend('7d', 'output');

      expect(hits[0].url).toBe(ENDPOINT_TREND);
      expect(hits[0].params.bucket).toBe('hour');
      expect(hits[0].params.metric).toBe('input');
      expect(hits[1].params.bucket).toBe('day');
      expect(hits[1].params.metric).toBe('output');
    });

    it('三种口径取对应的 token 字段，另外三个量进 detail；刻度按平台时区', async () => {
      routes[ENDPOINT_TREND] = () => ({
        points: [
          {
            bucket_start: '2026-10-18T12:00:00Z',
            total_tokens: 30,
            prompt_tokens: 20,
            completion_tokens: 10,
            call_count: 2,
          },
        ],
      });

      const total = await getTokenTrend('7d', 'total');
      const input = await getTokenTrend('7d', 'input');
      const output = await getTokenTrend('7d', 'output');

      expect(total[0].value).toBe(30);
      expect(input[0].value).toBe(20);
      expect(output[0].value).toBe(10);
      expect(total[0].detail).toEqual({ input: 20, output: 10, calls: 2 });
      // UTC 12:00 = 北京 20:00；7d 是 day 桶
      expect(total[0].date).toBe('10-18');
    });
  });

  describe('模型使用分布', () => {
    it('limit=5，count 取 total_tokens；零用量模型不进分布', async () => {
      routes[ENDPOINT_TOP] = () => ({
        items: [
          { model_id: 'deepseek/deepseek-flash', total_tokens: 80, ratio: 0.8 },
          { model_id: 'Qwen3-Embedding-8B', total_tokens: 20, ratio: 0.2 },
          // 时间窗内没有用量也照样出现在 TOP N 里（后端按 model 分组、不筛 0）
          { model_id: 'deepseek-v4-flash-202605', total_tokens: 0, ratio: 0 },
        ],
      });

      const share = await getModelShare('7d');

      expect(hits[0].url).toBe(ENDPOINT_TOP);
      expect(hits[0].params.limit).toBe(5);
      expect(share).toEqual([
        { key: 'deepseek/deepseek-flash', label: 'deepseek/deepseek-flash', count: 80 },
        { key: 'Qwen3-Embedding-8B', label: 'Qwen3-Embedding-8B', count: 20 },
      ]);
    });
  });

  describe('模型用量明细', () => {
    it('默认 page=1 / page_size=5；关键字去空格后下发，空关键字不传', async () => {
      routes[ENDPOINT_USAGE] = () => ({ items: [], total: 0 });

      await getModelUsage('7d', '   ');
      await getModelUsage('7d', '  qwen  ', 2, 10, { field: 'call_count', order: 'asc' });

      expect(hits[0].url).toBe(ENDPOINT_USAGE);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(5);
      expect('keyword' in hits[0].params).toBe(false);
      expect('sort_by' in hits[0].params).toBe(false);
      expect(hits[1].params).toMatchObject({
        keyword: 'qwen',
        page: 2,
        page_size: 10,
        sort_by: 'call_count',
        sort_order: 'asc',
      });
    });

    it('字段映射：prompt/completion → input/output；空均值保留 null，时间落占位', async () => {
      routes[ENDPOINT_USAGE] = () => ({
        items: [
          {
            model_id: 'm',
            model_name: 'm',
            call_count: 3,
            prompt_tokens: 10,
            completion_tokens: 5,
            total_tokens: 15,
            avg_tokens_per_call: null,
            last_called_at: null,
            unmetered_count: 1,
          },
          {
            model_id: 'm2',
            model_name: 'm2',
            call_count: 1,
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0,
            avg_tokens_per_call: 0,
            last_called_at: '2026-10-13T08:40:00Z',
            // 老后端不带该字段
          },
        ],
        total: 9,
      });

      const page = await getModelUsage('7d', '', 1);

      expect(page.total).toBe(9);
      expect(page.rows[0]).toMatchObject({
        name: 'm',
        modelId: 'm',
        calls: 3,
        inputTokens: 10,
        outputTokens: 5,
        totalTokens: 15,
        avgTokensPerCall: null,
        lastCallAt: '-',
        unmeteredCount: 1,
      });
      expect(page.rows[1].unmeteredCount).toBe(0);
      expect(page.rows[1].lastCallAt).toBe('2026-10-13 16:40:00');
    });
  });
});
