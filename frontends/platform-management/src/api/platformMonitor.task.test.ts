/**
 * 任务监控取数层（统计卡 / 趋势 / 失败列表 / 运行概览 / 筛选元数据）—— 契约与映射验收。
 *
 * 与 `platformMonitor.call.test.ts` 同一套手法：用**真实的 axios 实例**，只把 adapter（传输层）换掉，
 * 于是「网线之外」的东西（拦截器、信封校验、`unwrap` 解包）全走生产代码。断言钉四件事：
 *   ① **路径与入参**：任务监控与筛选元数据接口打契约路径，分页/时间参数照约定拼装；
 *   ② **单位换算**：后端给 **0–1 小数比率** 与 **毫秒**，前端类型要 **百分数** 与 **秒**；
 *   ③ **null 透传**：空窗口的比率/均值后端返 `null`，映射层不得兜成 0；
 *   ④ **时区族**：任务族接口给 `+08:00`，格式化必须原样落在平台时区，不从浏览器时区再偏移。
 *
 * 边界：本文件只测「接口 JSON → types.ts」这一层，不渲染 React（页面状态属组件测试的范围）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import { getMetaFilters, getTaskFailures, getTaskOverview, getTaskStats, getTaskTrend } from './platformMonitor';

const ENDPOINT_SUMMARY = '/platform/monitor/tasks/summary';
const ENDPOINT_TREND = '/platform/monitor/tasks/trend';
const ENDPOINT_FAILURES = '/platform/monitor/tasks/failures';
const ENDPOINT_OVERVIEW = '/platform/monitor/tasks/overview';
const ENDPOINT_META = '/platform/monitor/meta/filters';

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
  // ⚠ 必须同时注入 window：shared-lib 的 getStorage() 以 typeof window === 'undefined' 判空
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

describe('任务监控取数层契约与映射', () => {
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
    it('打契约路径 · 时间参数为 from/to/range_preset', async () => {
      routes[ENDPOINT_SUMMARY] = () => ({
        total_tasks: 13900,
        success_rate: 0.95,
        avg_duration_ms: 196000,
        waiting_now: 174,
        incomplete_count: 3,
        as_of: '2026-10-13T08:00:00+08:00',
      });

      await getTaskStats('24h');

      expect(hits).toHaveLength(1);
      expect(hits[0].url).toBe(ENDPOINT_SUMMARY);
      expect(hits[0].params.range_preset).toBe('24h');
      // from/to 必填且是合法 ISO8601；具体值随「现在」走，这里只钉住可解析
      expect(Number.isNaN(Date.parse(hits[0].params.from as string))).toBe(false);
      expect(Number.isNaN(Date.parse(hits[0].params.to as string))).toBe(false);
    });

    it('比率 ×100、毫秒 ÷1000、waiting_now 直取', async () => {
      routes[ENDPOINT_SUMMARY] = () => ({
        total_tasks: 13900,
        success_rate: 0.95,
        avg_duration_ms: 196400,
        waiting_now: 174,
        incomplete_count: 3,
        as_of: '2026-10-13T08:00:00+08:00',
      });

      const stats = await getTaskStats('24h');

      expect(stats.total).toBe(13900);
      expect(stats.successRate).toBeCloseTo(95, 6);
      expect(stats.avgDurationSec).toBe(196);
      expect(stats.waiting).toBe(174);
      expect(stats.asOf).toBe('2026-10-13T08:00:00+08:00');
    });

    it('空窗口：比率 / 均值保持 null（禁兜 0）', async () => {
      routes[ENDPOINT_SUMMARY] = () => ({
        total_tasks: 0,
        success_rate: null,
        avg_duration_ms: null,
        waiting_now: 0,
        incomplete_count: 0,
        as_of: '',
      });

      const stats = await getTaskStats('24h');

      expect(stats.successRate).toBeNull();
      expect(stats.avgDurationSec).toBeNull();
    });

    it('flags.module_error 落到 moduleError，供页面与「没数据」区分', async () => {
      routes[ENDPOINT_SUMMARY] = () => ({
        total_tasks: 0,
        success_rate: null,
        avg_duration_ms: null,
        waiting_now: 0,
        incomplete_count: 0,
        as_of: '',
        flags: { data_delayed: false, demo_data: false, module_error: '本模块暂不可用' },
      });

      const stats = await getTaskStats('24h');

      expect(stats.moduleError).toBe('本模块暂不可用');
    });
  });

  describe('趋势', () => {
    it('metric 改名 + 24h 用 bucket=hour', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTaskTrend('24h', 'volume');

      expect(hits[0].url).toBe(ENDPOINT_TREND);
      expect(hits[0].params.metric).toBe('task_count');
      expect(hits[0].params.bucket).toBe('hour');
      expect(hits[0].params.range_preset).toBe('24h');
    });

    it('1h / 24h 用 hour 桶，7d / 30d 用 day 桶', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTaskTrend('1h', 'volume');
      await getTaskTrend('24h', 'volume');
      await getTaskTrend('7d', 'volume');
      await getTaskTrend('30d', 'volume');

      expect(hits.map((h) => h.params.bucket)).toEqual(['hour', 'hour', 'day', 'day']);
    });

    it('四档指标逐一对上契约取值（与调用侧命名不同）', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTaskTrend('7d', 'volume');
      await getTaskTrend('7d', 'successRate');
      await getTaskTrend('7d', 'avgDuration');
      await getTaskTrend('7d', 'failures');

      expect(hits.map((h) => h.params.metric)).toEqual([
        'task_count',
        'success_rate',
        'avg_duration',
        'failed_count',
      ]);
    });

    it('成功率点 ×100、耗时点 ÷1000；空桶 null 原样透传', async () => {
      routes[ENDPOINT_TREND] = () => ({
        points: [
          { bucket_start: '2026-10-13T00:00:00+08:00', value: 0.8, closed: true },
          { bucket_start: '2026-10-13T01:00:00+08:00', value: null, closed: false },
        ],
      });

      const pct = await getTaskTrend('24h', 'successRate');
      expect(pct[0].value).toBeCloseTo(80, 6);
      expect(pct[1].value).toBeNull();

      routes[ENDPOINT_TREND] = () => ({
        points: [{ bucket_start: '2026-10-13T00:00:00+08:00', value: 168000, closed: true }],
      });
      const dur = await getTaskTrend('24h', 'avgDuration');
      expect(dur[0].value).toBe(168);

      routes[ENDPOINT_TREND] = () => ({
        points: [{ bucket_start: '2026-10-13T00:00:00+08:00', value: 62, closed: true }],
      });
      const vol = await getTaskTrend('24h', 'volume');
      expect(vol[0].value).toBe(62);
    });

    it('任务族 +08:00 刻度原样出字（不再按浏览器时区偏移）', async () => {
      routes[ENDPOINT_TREND] = () => ({
        points: [{ bucket_start: '2026-10-13T16:00:00+08:00', value: 1, closed: true }],
      });

      const points = await getTaskTrend('24h', 'volume');

      expect(points[0].date).toBe('16:00');
    });

    it('按任务类型筛趋势：选了才传 task_type，全部任务时不传', async () => {
      routes[ENDPOINT_TREND] = () => ({ points: [] });

      await getTaskTrend('24h', 'volume', 'file_processing_ontology');
      await getTaskTrend('24h', 'volume');

      expect(hits[0].params.task_type).toBe('file_processing_ontology');
      expect('task_type' in hits[1].params).toBe(false);
    });
  });

  describe('失败列表', () => {
    it('恒传 page=1 / page_size=5，返回 {items, total}', async () => {
      routes[ENDPOINT_FAILURES] = () => ({ items: [], total: 0 });

      const page = await getTaskFailures('24h');

      expect(hits[0].url).toBe(ENDPOINT_FAILURES);
      expect(hits[0].params.page).toBe(1);
      expect(hits[0].params.page_size).toBe(5);
      expect(page.items).toEqual([]);
      expect(page.total).toBe(0);
    });

    it('task_type 选了才传；total 直接取接口值', async () => {
      routes[ENDPOINT_FAILURES] = () => ({ items: [], total: 42 });

      const page = await getTaskFailures('7d', 'file_processing_ontology');

      expect(hits[0].params.task_type).toBe('file_processing_ontology');
      expect(page.total).toBe(42);

      await getTaskFailures('7d');
      expect('task_type' in hits[1].params).toBe(false);
    });

    it('映射：reason 直取、时间按平台时区；task_name 缺失/为空时落 "-"', async () => {
      routes[ENDPOINT_FAILURES] = () => ({
        items: [
          {
            task_id: '#T20260920321',
            task_name: '金融知识库文档处理',
            task_type: 'file_processing_ontology',
            status: 'failed',
            error_message: '编译失败',
            finished_at: '2026-10-13T08:40:00+08:00',
            reason: 'compile',
          },
          {
            task_id: '#T20260920308',
            task_name: null,
            task_type: 'file_processing_ontology',
            status: 'failed',
            error_message: null,
            finished_at: null,
            reason: 'ontology',
          },
        ],
        total: 2,
      });

      const page = await getTaskFailures('24h');

      expect(page.items).toHaveLength(2);
      expect(page.items[0].taskId).toBe('#T20260920321');
      expect(page.items[0].name).toBe('金融知识库文档处理');
      expect(page.items[0].reason).toBe('compile');
      expect(page.items[0].time).toBe('10-13 08:40');
      // 缺失任务名落占位符（空串读起来像「加载失败」）；缺失时刻同样落 "-"
      expect(page.items[1].name).toBe('-');
      expect(page.items[1].time).toBe('-');
    });
  });

  describe('运行概览', () => {
    it('父行与嵌套 stages 同款换算，缺 stages 不崩', async () => {
      routes[ENDPOINT_OVERVIEW] = () => ({
        items: [
          {
            task_type: 'file_processing_ontology',
            total_count: 8640,
            running_count: 42,
            waiting_count: 128,
            failed_count: 312,
            success_rate: 0.962,
            avg_duration_ms: 258000,
            stages: [
              {
                stage_code: 'parse',
                stage_name: '文件解析',
                exec_count: 100,
                running_count: 18,
                waiting_count: 54,
                failed_count: 96,
                success_rate: 0.974,
                avg_duration_ms: 62000,
              },
            ],
          },
          {
            task_type: 'file_processing_jonex_wiki',
            total_count: 1,
            running_count: 0,
            waiting_count: 0,
            failed_count: 0,
            success_rate: 1,
            avg_duration_ms: null,
          },
        ],
      });

      const rows = await getTaskOverview('24h');

      expect(hits[0].url).toBe(ENDPOINT_OVERVIEW);
      expect(rows).toHaveLength(2);
      expect(rows[0].taskType).toBe('file_processing_ontology');
      expect(rows[0].total).toBe(8640);
      expect(rows[0].successRate).toBeCloseTo(96.2, 6);
      expect(rows[0].avgDurationSec).toBe(258);
      expect(rows[0].stages[0].stage).toBe('parse');
      // 展示名走后端 stage_name（stage_code 是可增的目录值，前端枚举兜不住）
      expect(rows[0].stages[0].stageName).toBe('文件解析');
      expect(rows[0].stages[0].successRate).toBeCloseTo(97.4, 6);
      expect(rows[0].stages[0].avgDurationSec).toBe(62);
      // 缺 stages 时按空数组处理，不崩
      expect(rows[1].stages).toEqual([]);
      expect(rows[1].avgDurationSec).toBeNull();
    });

    it('空窗口：父行与 stage 的比率 / 均值都保持 null', async () => {
      routes[ENDPOINT_OVERVIEW] = () => ({
        items: [
          {
            task_type: 'document',
            total_count: 0,
            running_count: 0,
            waiting_count: 0,
            failed_count: 0,
            success_rate: null,
            avg_duration_ms: null,
            stages: [
              {
                stage_code: 'parse',
                stage_name: '文件解析',
                exec_count: 0,
                running_count: 0,
                waiting_count: 0,
                failed_count: 0,
                success_rate: null,
                avg_duration_ms: null,
              },
            ],
          },
        ],
      });

      const rows = await getTaskOverview('24h');

      expect(rows[0].successRate).toBeNull();
      expect(rows[0].avgDurationSec).toBeNull();
      expect(rows[0].stages[0].successRate).toBeNull();
      expect(rows[0].stages[0].avgDurationSec).toBeNull();
    });
  });

  describe('筛选元数据', () => {
    it('task_types 保留 code 与中文名，statuses 的 code 可为 null', async () => {
      routes[ENDPOINT_META] = () => ({
        task_types: [
          { code: 'file_processing_ontology', name: '文件处理（Ontology）' },
          { code: 'file_processing_jonex_wiki', name: '文件处理（Jonex Wiki）' },
        ],
        statuses: [
          { code: 'failed', label: '失败', pm_label: '失败', in_denominator: true },
          // 「超时」档没有可下发的筛选码，后端给 null —— 不得被过滤或改成空串
          { code: null, label: '超时', pm_label: '超时', in_denominator: false },
        ],
        objects: [{ object_type: 'openai', protocol: 'openai' }],
        call_sources: ['task-center'],
      });

      const meta = await getMetaFilters();

      expect(hits[0].url).toBe(ENDPOINT_META);
      expect(meta?.taskTypes).toEqual([
        { code: 'file_processing_ontology', name: '文件处理（Ontology）' },
        { code: 'file_processing_jonex_wiki', name: '文件处理（Jonex Wiki）' },
      ]);
      expect(meta?.statuses[1]).toEqual({
        code: null,
        label: '超时',
        pmLabel: '超时',
        inDenominator: false,
      });
      expect(meta?.objects).toEqual([{ objectType: 'openai', protocol: 'openai' }]);
      expect(meta?.callSources).toEqual(['task-center']);
    });

    it('接口不可用时返回 null 而不是抛（下拉候选不能拖垮整页）', async () => {
      delete routes[ENDPOINT_META];

      await expect(getMetaFilters()).resolves.toBeNull();
    });
  });
});
