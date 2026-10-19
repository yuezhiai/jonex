/**
 * `tenantSecurity` 取数模块 —— 租户安全设置 4 项的读写。
 *
 * 与 `frontends/shell/src/api/uiConfig.test.ts` 同一套手法：用**真实的 axios 实例**，
 * 只把 adapter（传输层）换掉 —— 于是请求/响应拦截器、信封校验、`unwrap` 解包全走生产
 * 代码，被替换的只有「网线」。断言钉住三件事：
 *   ① 打的是 `/platform/tenant-security-config`（不是平台管理员的 system-configs）；
 *   ② 信封被解包（拿到的是 data，不是 `{success,code,data}`）；
 *   ③ 5xx / 业务失败**抛错不吞** —— 页面才能拿到 message 提示用户。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import { getTenantSecurityConfig, resetTenantSecurityConfig, updateTenantSecurityConfig } from './tenantSecurity';
import type { TenantSecurityConfigItem } from './tenantSecurity';

/** 后端契约路径（与 platform 能力侧路由一致） */
const ENDPOINT = '/platform/tenant-security-config';

/** 内存版 localStorage：请求拦截器要读 locale，node 环境没有原生实现 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
  // ⚠ 必须同时注入 window：shell-sdk 的 getStorage() 以 typeof window === 'undefined'
  // 判空（authStorage.ts:10-13），只 stub globalThis.localStorage 会让 token 读写静默落空。
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

function stubAdapter(adapter: AxiosAdapter): void {
  adapterSlot().adapter = adapter;
}

/** 单条记录的构造器：只写关心的字段，其余给中性默认值 */
function item(overrides: Partial<TenantSecurityConfigItem>): TenantSecurityConfigItem {
  return {
    config_key: 'session_timeout',
    config_value: '30',
    value_type: 'int',
    description: null,
    platform_value: '30',
    is_overridden: false,
    // 合法范围由后端随 item 下发（见 capabilities/platform/services/security_config_service.py
    // 的 SECURITY_CONFIG_DEFAULTS），前端不另写一份
    min: 1,
    max: 10080,
    ...overrides,
  };
}

/** 记录本次请求的 url / method / body（body 是 axios 序列化后的 JSON 串） */
let requests: Array<{ url: string; method: string; data: unknown }> = [];

/** 包一层：记录请求后交给行为桩 */
function recording(fn: AxiosAdapter): AxiosAdapter {
  return (config) => {
    requests.push({
      url: String(config.url ?? ''),
      method: String(config.method ?? '').toLowerCase(),
      data: config.data,
    });
    return fn(config);
  };
}

const originalAdapter = apiClient.raw.defaults.adapter;

describe('tenantSecurity · 租户安全设置取数', () => {
  beforeEach(() => {
    installLocalStorage();
    requests = [];
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  // ① GET 正常解包：拿到 data.items（当前生效值 + 平台默认值 + 是否覆盖）
  it('GET 返回信封 ⇒ 解包出 items，且打的是 tenant-security-config', async () => {
    const items = [
      item({ config_key: 'session_timeout', config_value: '60', platform_value: '30', is_overridden: true }),
      // password_min_length 的后端上界是 72（bcrypt 截断），前端不做二次裁剪、原样透传
      item({ config_key: 'password_min_length', config_value: '8', platform_value: '8', min: 1, max: 72 }),
      item({ config_key: 'login_lock_threshold', config_value: '5', platform_value: '5' }),
      item({ config_key: 'lock_duration', config_value: '15', platform_value: '15' }),
    ];
    stubAdapter(recording(async (config) => makeResponse(config, { success: true, code: 0, data: { items } })));

    const res = await getTenantSecurityConfig();

    expect(res.items).toHaveLength(4);
    // 关键字段没被信封包着：拿到的是 data.items[0]，不是整个 {success,code,data}
    expect(res.items[0]).toMatchObject({
      config_key: 'session_timeout',
      config_value: '60',
      platform_value: '30',
      is_overridden: true,
    });
    // 合法范围随 item 下发（页面校验与 InputNumber 的 min/max 都用它）
    expect(res.items[0].min).toBe(1);
    expect(res.items[0].max).toBe(10080);
    expect(res.items[1]).toMatchObject({ config_key: 'password_min_length', min: 1, max: 72 });
    expect(requests).toHaveLength(1);
    expect(requests[0].method).toBe('get');
    expect(requests[0].url).toBe(ENDPOINT);
    // 不能误打到平台管理员的 system-configs（两个页面、两套权限码）
    expect(requests[0].url).not.toContain('system-config');
  });

  // ② PUT 成功后返回项：url 带 config_key、body 是 {config_value}
  it('PUT 覆盖值 ⇒ 返回更新后的单项，body 只带 config_value', async () => {
    const updated = item({
      config_key: 'session_timeout',
      config_value: '60',
      platform_value: '30',
      is_overridden: true,
    });
    stubAdapter(recording(async (config) => makeResponse(config, { success: true, code: 0, data: updated })));

    const res = await updateTenantSecurityConfig('session_timeout', '60');

    expect(res).toMatchObject({ config_key: 'session_timeout', config_value: '60', is_overridden: true });
    expect(requests[0].method).toBe('put');
    expect(requests[0].url).toBe(`${ENDPOINT}/session_timeout`);
    expect(JSON.parse(String(requests[0].data))).toEqual({ config_value: '60' });
  });

  it('PUT 带 description ⇒ body 一并下发（可选字段按需携带）', async () => {
    stubAdapter(recording(async (config) => makeResponse(config, { success: true, code: 0, data: item({}) })));

    await updateTenantSecurityConfig('password_min_length', '12', '密码最小长度');

    expect(JSON.parse(String(requests[0].data))).toEqual({
      config_value: '12',
      description: '密码最小长度',
    });
  });

  // ③ DELETE = 恢复平台默认：is_overridden 变回 false
  it('DELETE 覆盖值 ⇒ 恢复默认，返回 is_overridden=false 的单项', async () => {
    const reset = item({
      config_key: 'lock_duration',
      config_value: '15',
      platform_value: '15',
      is_overridden: false,
    });
    stubAdapter(recording(async (config) => makeResponse(config, { success: true, code: 0, data: reset })));

    const res = await resetTenantSecurityConfig('lock_duration');

    expect(res).toMatchObject({ config_key: 'lock_duration', config_value: '15', is_overridden: false });
    expect(requests[0].method).toBe('delete');
    expect(requests[0].url).toBe(`${ENDPOINT}/lock_duration`);
  });

  // ④ HTTP 5xx：抛错且保留 status/message，绝不静默吞成空配置
  it('HTTP 500 ⇒ 抛错不吞，保留后端 message 与 status', async () => {
    stubAdapter(
      recording(async (config) => {
        const response = makeResponse(config, { success: false, code: 50000, message: '配置服务不可用' }, 500);
        throw new AxiosError(
          'Request failed with status code 500',
          AxiosError.ERR_BAD_RESPONSE,
          config,
          undefined,
          response as never,
        );
      }),
    );

    await expect(getTenantSecurityConfig()).rejects.toThrow('配置服务不可用');

    // 再跑一次单独取错误对象：status 也要透出来（页面/调用方按状态区分 401/5xx）
    const err = (await getTenantSecurityConfig().catch((e: unknown) => e)) as Error & { status?: number };
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe('配置服务不可用');
    expect(err.status).toBe(500);
  });

  it('HTTP 500 且响应体不是 JSON 信封 ⇒ 仍抛错（回落 Request failed (500)）', async () => {
    stubAdapter(
      recording(async (config) => {
        const response = makeResponse(config, '<html>502 Bad Gateway</html>', 502);
        throw new AxiosError(
          'Request failed with status code 502',
          AxiosError.ERR_BAD_RESPONSE,
          config,
          undefined,
          response as never,
        );
      }),
    );

    await expect(updateTenantSecurityConfig('session_timeout', '60')).rejects.toThrow(/502/);
  });

  it('HTTP 200 但信封 code!==0 ⇒ 按业务失败抛错（不把错误当数据）', async () => {
    stubAdapter(
      recording(async (config) =>
        makeResponse(config, { success: false, code: 40300, message: '缺少 tenant:security:write 权限' }),
      ),
    );

    await expect(resetTenantSecurityConfig('session_timeout')).rejects.toThrow('缺少 tenant:security:write 权限');
  });
});
