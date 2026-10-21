/**
 * `tenants` 取数模块 —— 租户列表的关键字检索契约。
 *
 * 与 `tenantSecurity.test.ts` 同一套手法：用**真实的 axios 实例**，只把 adapter（传输层）换掉，
 * 于是拦截器、信封校验、`unwrap` 解包全走生产代码，被替换的只有「网线」。断言钉住两件事：
 *   ① 打的是 `/platform/tenants`，分页参数照旧（page / page_size）；
 *   ② `keyword` 只在**去掉首尾空白后非空**时才出现在 params 里 —— 空串/纯空白不传，
 *      免得后端把「清空搜索框」也当成一次过滤（后端对空关键字按「不过滤」处理）。
 *
 * 检索本身在**后端**完成（`capabilities/platform/repository/tenant_repository.py` 按 ID /
 * 名称 ILIKE）；前端不再拉一页上限数据本地过滤，所以这里不再有 `page_size` 被搜索态改写的行为。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import { listTenants } from './tenants';
import type { TenantItem, TenantListResponse } from './tenants';

/** 后端契约路径（与 platform 能力侧路由一致） */
const ENDPOINT = '/platform/tenants';

/** 内存版 localStorage：请求拦截器要读 locale / token，node 环境没有原生实现 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
  // ⚠ 必须同时注入 window：shell-sdk 的 getStorage() 以 typeof window === 'undefined' 判空
  vi.stubGlobal('localStorage', store);
  vi.stubGlobal('window', { localStorage: store });
}

/** 构造与真实 adapter 同形的 axios 响应壳 */
function makeResponse(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config,
  } as AxiosResponse;
}

type AdapterSlot = { adapter: AxiosAdapter | undefined };

function adapterSlot(): AdapterSlot {
  return apiClient.raw.defaults as unknown as AdapterSlot;
}

/** 记录本次请求的参数（只关心 url 与 params） */
let params: Array<Record<string, unknown>> = [];

function stubListAdapter(items: TenantItem[], total: number): void {
  const adapter: AxiosAdapter = async (config) => {
    params.push({ url: String(config.url ?? ''), ...((config.params ?? {}) as Record<string, unknown>) });
    return makeResponse(config, { success: true, code: 0, data: { items, total } });
  };
  adapterSlot().adapter = adapter;
}

function tenant(overrides: Partial<TenantItem> = {}): TenantItem {
  return {
    id: 'tenant_jonex_demo',
    name: 'Jonex默认租户',
    description: '默认租户',
    status: 1,
    plan_type: 'standard',
    expire_time: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

const originalAdapter = apiClient.raw.defaults.adapter;

describe('tenants · 租户列表关键字检索', () => {
  beforeEach(() => {
    installLocalStorage();
    params = [];
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  it('不带关键字 ⇒ params 只有分页，不打到 system-config 之类的别处', async () => {
    stubListAdapter([tenant()], 1);

    const res: TenantListResponse = await listTenants(2, 20);

    expect(res).toEqual({ items: [tenant()], total: 1 });
    expect(params).toHaveLength(1);
    expect(params[0]).toEqual({ url: ENDPOINT, page: 2, page_size: 20 });
    expect('keyword' in params[0]).toBe(false);
  });

  it('关键字原样透传 ⇒ params 带 keyword（分页不变）', async () => {
    stubListAdapter([tenant({ id: 'tenant_acme', name: 'Acme 租户' })], 1);

    await listTenants(1, 10, 'acme');

    expect(params[0]).toEqual({ url: ENDPOINT, page: 1, page_size: 10, keyword: 'acme' });
  });

  it('首尾空白先去掉再传 ⇒ 搜「acme 」与「acme」等价', async () => {
    stubListAdapter([], 0);

    await listTenants(1, 10, '  acme  ');

    expect(params[0].keyword).toBe('acme');
  });

  it('纯空白 / 空串 ⇒ 不传 keyword（清空搜索框不该变成一次过滤）', async () => {
    stubListAdapter([tenant()], 1);

    await listTenants(1, 10, '   ');
    await listTenants(1, 10, '');

    expect(params).toHaveLength(2);
    expect('keyword' in params[0]).toBe(false);
    expect('keyword' in params[1]).toBe(false);
  });

  it('按租户 ID 检索时 keyword 就是那个 ID（大小写不敏感交给后端 ILIKE）', async () => {
    stubListAdapter([tenant({ id: 'tenant_jonex_beta' })], 1);

    const res = await listTenants(1, 10, 'tenant_jonex_beta');

    expect(params[0].keyword).toBe('tenant_jonex_beta');
    expect(res.items[0].id).toBe('tenant_jonex_beta');
  });
});
