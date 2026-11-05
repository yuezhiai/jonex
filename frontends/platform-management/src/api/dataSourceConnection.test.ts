/**
 * 连接池客户端的**只读凭据**口径。
 *
 * 背景：② NL→SQL 只认 `readonly_user` / `readonly_password_ref`，**绝不回退**到
 * 写账号（`capabilities/knowledge_base/services/db_config.py`）。以前连接弹窗没有
 * 这两个字段，登记只读账号只能走接口；这里钉住前端把表单值原样送进 `config_json`
 * 的三个口径：
 *   1. 填了就带（明文密码由后端 `encrypt_config` 换成引用）；
 *   2. 没填就不带（创建时不留空键）；
 *   3. 编辑时**清空只读账号 → 显式送空串**（后端 merge 语义，不送就删不掉）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios';
import apiClient from './request';
import { createConnection, updateConnection, type ConnectionPayload } from './dataSourceConnection';

function installLocalStorage(): void {
  const map = new Map<string, string>();
  const store = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => void map.clear(),
  };
  vi.stubGlobal('localStorage', store);
  vi.stubGlobal('window', { localStorage: store });
}

function makeResponse(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return { data, status, statusText: 'OK', headers: {}, config } as AxiosResponse;
}

type AdapterSlot = { adapter: AxiosAdapter | undefined };

function adapterSlot(): AdapterSlot {
  return apiClient.raw.defaults as unknown as AdapterSlot;
}

/** 抓最近一次请求的 body（axios 在进 adapter 前已把 data 序列化成字符串） */
let bodies: Array<Record<string, unknown>> = [];

function stubWriteAdapter(): void {
  const adapter: AxiosAdapter = async (config) => {
    const raw = config.data;
    bodies.push(typeof raw === 'string' ? JSON.parse(raw) : ((raw ?? {}) as Record<string, unknown>));
    return makeResponse(config, { success: true, code: 0, data: { id: 'conn_1', name: 'x' } });
  };
  adapterSlot().adapter = adapter;
}

const originalAdapter = apiClient.raw.defaults.adapter;

function basePayload(overrides: Partial<ConnectionPayload> = {}): ConnectionPayload {
  return {
    name: 'demo_dw',
    engine: 'postgresql',
    host: 'postgres',
    port: 5432,
    database: 'jonex',
    user: 'jonex',
    ...overrides,
  };
}

describe('dataSourceConnection · 只读凭据', () => {
  beforeEach(() => {
    installLocalStorage();
    bodies = [];
    stubWriteAdapter();
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  it('创建时填了只读账号+密码 ⇒ 两个键都进 config_json', async () => {
    await createConnection(
      basePayload({ readonlyUser: 'demo_wte_ro', readonlyPassword: 'ro_pwd' }),
      'space_demo',
    );

    expect(bodies).toHaveLength(1);
    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect(cfg).toMatchObject({
      user: 'jonex',
      readonly_user: 'demo_wte_ro',
      readonly_password: 'ro_pwd',
    });
  });

  it('创建时不填只读账号 ⇒ config_json 里没有这两个键（不留空壳）', async () => {
    await createConnection(basePayload(), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect('readonly_user' in cfg).toBe(false);
    expect('readonly_password' in cfg).toBe(false);
  });

  it('编辑时清空只读账号 ⇒ 显式送空串（后端 merge，不送删不掉）', async () => {
    await updateConnection('conn_1', basePayload({ readonlyUser: '' }), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect(cfg.readonly_user).toBe('');
  });

  it('编辑时只读密码留空 ⇒ 不带该键（后端保留原密码）', async () => {
    await updateConnection('conn_1', basePayload({ readonlyUser: 'demo_wte_ro' }), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect(cfg.readonly_user).toBe('demo_wte_ro');
    expect('readonly_password' in cfg).toBe(false);
  });
});

describe('dataSourceConnection · 该账号即只读账号', () => {
  beforeEach(() => {
    installLocalStorage();
    bodies = [];
    stubWriteAdapter();
  });

  afterEach(() => {
    adapterSlot().adapter = originalAdapter as AxiosAdapter;
    vi.unstubAllGlobals();
  });

  it('勾选后只写开关，不写显式只读凭据（即使调用方同时传了）', async () => {
    await createConnection(basePayload({
      readonlyUseMain: true,
      readonlyUser: 'ignored',
      readonlyPassword: 'ignored',
    }), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect(cfg.readonly_use_main).toBe(true);
    expect('readonly_user' in cfg).toBe(false);
    expect('readonly_password' in cfg).toBe(false);
  });

  it('取消勾选要显式送 false（merge 语义，不送关不掉）', async () => {
    await updateConnection('conn_1', basePayload({ readonlyUseMain: false }), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect(cfg.readonly_use_main).toBe(false);
  });

  it('未传开关时不写该键（兼容旧调用方，保留后端存量值）', async () => {
    await createConnection(basePayload(), 'space_demo');

    const cfg = bodies[0].config_json as Record<string, unknown>;
    expect('readonly_use_main' in cfg).toBe(false);
  });
});
