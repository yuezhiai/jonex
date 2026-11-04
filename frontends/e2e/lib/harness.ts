/**
 * 模拟租户会话续期隔离 —— E2E 量具（harness）
 *
 * 契约：[docs/auth/tenant-impersonation-refresh-isolation-plan.md] §4 BDD 行为规格、§5 ATDD 验收清单。
 * 本目录（frontends/e2e）只放验收用例，⛔ 不改任何产品代码。
 *
 * ⛔ 量具纪律（本仓已踩过的假绿坑，改本文件前先读 memory/browser-e2e-verification-playwright.md
 *    与 memory/session-2026-09-21-antd-v6-toast-detector-false-green.md）：
 *  1. antd 是 v6，两字按钮名会被插空格 ⇒ 一律不用按钮文案定位，改用 data-testid。
 *  2. 「UI 未出现某提示」类断言在 antd v6 下可能恒真 ⇒ 判「未出现」必须配正对照
 *     （同用例里先证明检测器能测到该元素出现）。
 *  3. Array.isArray([]) 恒真 ⇒ 不写空洞断言，长度/元素逐个断言。
 *  4. Playwright headless 下导航挂起时 page.evaluate 会被阻塞 ⇒ 不在导航在途时 evaluate；
 *     观测用 page.on('framenavigated') 累积跳转链，不用事后读 location.href。
 */
import { expect, type Locator, type Page } from '@playwright/test';

/** 与 frontends/shell-sdk/src/autoRefresh.ts 的 REFRESH_URL 保持一致 */
export const REFRESH_PATH = '/api/v1/auth/refresh';

export const STORAGE = {
  access: 'jonex_access_token',
  backup: 'jonex_impersonation_backup',
  expiresAt: 'jonex_token_expires_at',
  user: 'jonex_user',
} as const;

/**
 * ⛔ 被测元素契约（产品代码里已补齐；缺一个就会显式 FAIL，不会静默跳过）。
 *
 * 用 testid 而非文案：antd v6 会在两字按钮名里插空格，文案定位会假 FAIL。
 * 也**不用** `data-menu-id` / `role=menuitem` 之类回退 —— 那些是 antd 的内部实现细节，
 * 升级即变；回退链已**退役**（定位只剩 testid 一条路）。见 memory/browser-e2e-verification-playwright.md。
 */
export const TESTID = {
  /** 顶栏「切换租户」触发器 */
  switcher: 'tenant-impersonation-switcher',
  /** 下拉里每个目标租户的选项，按租户 ID 区分 */
  switcherOption: (tenantId: string) => `tenant-impersonation-option-${tenantId}`,
  /** 模拟态顶部常驻横幅（复用 AppShellLayout 的 Alert） */
  banner: 'impersonation-banner',
  /** 横幅内的倒计时节点：只在最后 60 秒出现 */
  countdown: 'impersonation-countdown',
} as const;

export const MISSING_TESTID_HINT = [
  '定位不到被测元素：data-testid 缺失（产品代码没补上，或被改名/改位置）。',
  '⛔ 不要改成按按钮文案定位 —— antd v6 会给两字按钮名插空格（假 FAIL）。',
  '⛔ 也不要回退 data-menu-id / role=menuitem —— 那是 antd 内部实现细节，升级即变。',
  `契约：${TESTID.switcher} / ${TESTID.switcherOption('<tenantId>')} / ${TESTID.banner} / ${TESTID.countdown}`,
  '正解是让产品代码补回同名 testid，不是把定位改脆。',
].join('\n');

export const EVENTS = {
  tokenExpired: 'jonex:token-expired',
  impersonationExpired: 'jonex:impersonation-expired',
} as const;

const EVENTS_KEY = '__jonex_e2e_events';
const TOASTS_KEY = '__jonex_e2e_toasts';

// ---------------------------------------------------------------- 配置

export function cfg() {
  const expireMinutes = Number(process.env.E2E_IMPERSONATE_EXPIRE_MINUTES || '2');
  return {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:5173',
    tenant: process.env.E2E_TENANT || 'tenant_jonex_demo',
    username: process.env.E2E_USERNAME || '',
    password: process.env.E2E_PASSWORD || '',
    targetTenant: process.env.E2E_TARGET_TENANT || 'tenant_jonex_sso',
    /** 必须与后端 IMPERSONATE_EXPIRE_MINUTES 一致，否则 S2/S3 的时间点算错 */
    impersonateExpireMinutes: expireMinutes,
    /** S1 观察窗口：模拟态下「一次 refresh 都不能有」的观测时长 */
    s1ObserveMs: Number(process.env.E2E_S1_OBSERVE_MS || '15000'),
    auditAction: process.env.E2E_AUDIT_ACTION || 'impersonate_expired',
    /** 用于制造业务 401 的接口：默认 /auth/me（应用启动必经的 axios 调用） */
    probe401Pattern: process.env.E2E_401_PROBE_PATTERN || '**/api/v1/auth/me',
    toastSelector: process.env.E2E_TOAST_SELECTOR || '.ant-message-notice',
    /** 到期提示文案（可能中英双语），用正则匹配 */
    toastTextPattern: new RegExp(process.env.E2E_TOAST_TEXT_PATTERN || '模拟|impersonat', 'i'),
  };
}

// ---------------------------------------------------------------- JWT / storage

export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const part = token.split('.')[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    return JSON.parse(Buffer.from(padded, 'base64').toString('utf8')) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export interface StorageSnapshot {
  accessToken: string | null;
  backup: string | null;
  expiresAt: string | null;
  user: string | null;
}

export async function readStorage(page: Page): Promise<StorageSnapshot> {
  return page.evaluate(
    (keys) => ({
      accessToken: localStorage.getItem(keys.access),
      backup: localStorage.getItem(keys.backup),
      expiresAt: localStorage.getItem(keys.expiresAt),
      user: localStorage.getItem(keys.user),
    }),
    STORAGE,
  );
}

/** 断言：当前 localStorage 里的 access token 属于某租户，且（可选）处于/不处于模拟态 */
export async function expectTokenTenant(
  page: Page,
  tenantId: string,
  impersonated: boolean,
  label = '',
): Promise<Record<string, unknown>> {
  const s = await readStorage(page);
  expect(s.accessToken, `${label} 应有 access token`).toBeTruthy();
  const payload = decodeJwtPayload(s.accessToken as string);
  expect(payload, `${label} access token 应为可解码 JWT (base64url payload)`).toBeTruthy();
  expect(payload!.tenant_id, `${label} access token 的 tenant_id`).toBe(tenantId);
  expect(!!payload!.impersonated, `${label} access token 的 impersonated 标记`).toBe(impersonated);
  return payload!;
}

// ---------------------------------------------------------------- 采集器

export interface RequestHit {
  url: string;
  method: string;
}

export interface NetworkRecorder {
  /** 观测窗口内所有请求数（用于证明采集器在窗口内一直是活的） */
  all: RequestHit[];
  /** /api/v1/auth/refresh 请求（本单的 S1 判据） */
  refresh: RequestHit[];
  /** 命中 401 探针 pattern 的业务请求（S9 反空洞用） */
  probe: RequestHit[];
  /** 带 Bearer 的请求（S3「后续请求的 Bearer 属于原租户」用） */
  bearer: { url: string; token: string }[];
}

/** 监听网络。⚠️ page.on('request') 挂在 Page 上，跨 reload 持续有效。 */
export function recordNetwork(page: Page, probePattern?: string | RegExp): NetworkRecorder {
  const probe =
    probePattern instanceof RegExp
      ? probePattern
      : new RegExp(String(probePattern || cfg().probe401Pattern).replace(/\*\*/g, '.*'));
  const rec: NetworkRecorder = { all: [], refresh: [], probe: [], bearer: [] };
  page.on('request', (req) => {
    const url = req.url();
    const method = req.method();
    rec.all.push({ url, method });
    if (url.includes(REFRESH_PATH)) rec.refresh.push({ url, method });
    if (probe.test(url)) rec.probe.push({ url, method });
    const auth = req.headers()['authorization'];
    if (auth && auth.startsWith('Bearer ')) rec.bearer.push({ url, token: auth.slice(7) });
  });
  return rec;
}

/**
 * 记录主框架跳转链。
 * ⛔ 不要用事后 page.evaluate(() => location.href) —— 导航挂起时会被阻塞、读到过期状态
 *    （见 memory/browser-e2e-verification-playwright.md 的「致命坑」）。
 */
export function recordNavigations(page: Page): string[] {
  const urls: string[] = [];
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) urls.push(frame.url());
  });
  return urls;
}

export function navigatedToLogin(urls: string[]): boolean {
  return urls.some((u) => /\/login(\?|\/|$)/.test(u));
}

/**
 * 事件采集器（页面内）。挂在 addInitScript ⇒ 每次导航都会重新安装。
 * ⚠️ addInitScript 的函数会被序列化后送进浏览器，**外层作用域不存在** ⇒ 常量必须在函数体内重新声明。
 * 事件写入 sessionStorage ⇒ 跨 reload 累积（window 上的数组会被导航清掉）。
 *
 * ⚠️ addInitScript **只对之后的导航生效，不会注入当前已加载的文档**。
 * S9 的事件（模拟态 401 → impersonation-expired）发生在安装后的**同一文档**里，还原后才 reload；
 * 若只在 addInitScript 安装，事件会落在没有监听器的文档上 ⇒ 采集恒空（2026-10-26 实测踩坑，
 * 且会让「不得发 token-expired」在空数组上假绿）。故安装时对当前文档再注入一次。
 */
export async function installEventRecorder(page: Page, events: string[]): Promise<void> {
  const install = (names: string[]) => {
    const KEY = '__jonex_e2e_events';
    // 同一文档内 addInitScript 与「立即注入」会各装一次 ⇒ 用标记防重复注册
    const FLAG = '__jonex_e2e_event_recorder_ready';
    const flags = window as unknown as Record<string, unknown>;
    if (flags[FLAG]) return;
    flags[FLAG] = true;
    const load = (): string[] => {
      try {
        return JSON.parse(sessionStorage.getItem(KEY) || '[]') as string[];
      } catch {
        return [];
      }
    };
    for (const name of names) {
      window.addEventListener(name, () => {
        const all = load();
        all.push(name);
        try {
          sessionStorage.setItem(KEY, JSON.stringify(all));
        } catch {
          /* ignore */
        }
      });
    }
  };
  await page.addInitScript(install, events);
  await page.evaluate(install, events);
}

export async function readEvents(page: Page): Promise<string[]> {
  return page.evaluate((key) => {
    try {
      return JSON.parse(sessionStorage.getItem(key) || '[]') as string[];
    } catch {
      return [];
    }
  }, EVENTS_KEY);
}

/**
 * antd message 采集器（版本无关：`.ant-message-notice` 是 v5/v6 共有容器类名；
 * ⛔ 不要用 v5 的 `.ant-message-notice-content`，v6 已删除 ⇒ 恒返回空 ⇒ 空洞通过）。
 *
 * 记录的是**出现次数**（按节点计），不是去重后的文案集合 —— 否则「不再重复出现」这条
 * 断言会因为去重而恒真。无文本的节点不标记 seen（节点可能先挂载后填文本）。
 */
export async function installToastRecorder(page: Page): Promise<void> {
  const selector = cfg().toastSelector;
  await page.addInitScript((sel: string) => {
    const KEY = '__jonex_e2e_toasts';
    const load = (): { text: string; t: number }[] => {
      try {
        return JSON.parse(sessionStorage.getItem(KEY) || '[]') as { text: string; t: number }[];
      } catch {
        return [];
      }
    };
    const seen = new WeakSet<Node>();
    const flush = (all: { text: string; t: number }[]): void => {
      try {
        sessionStorage.setItem(KEY, JSON.stringify(all));
      } catch {
        /* ignore */
      }
    };
    const collect = (): void => {
      const all = load();
      let dirty = false;
      for (const node of Array.from(document.querySelectorAll(sel))) {
        if (seen.has(node)) continue;
        const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (!text) continue; // 无文本不标记，等它填上
        seen.add(node);
        all.push({ text, t: Date.now() });
        dirty = true;
      }
      if (dirty) flush(all);
    };
    const start = (): void => {
      collect();
      new MutationObserver(collect).observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      setInterval(collect, 250);
    };
    if (document.body) start();
    else document.addEventListener('DOMContentLoaded', start);
  }, selector);
}

export interface ToastHit {
  text: string;
  t: number;
}

export async function readToasts(page: Page): Promise<ToastHit[]> {
  return page.evaluate((key) => {
    try {
      return JSON.parse(sessionStorage.getItem(key) || '[]') as { text: string; t: number }[];
    } catch {
      return [];
    }
  }, TOASTS_KEY);
}

export async function writeToasts(page: Page, toasts: ToastHit[]): Promise<void> {
  await page.evaluate(
    ([key, value]) => {
      sessionStorage.setItem(key as string, value as string);
    },
    [TOASTS_KEY, JSON.stringify(toasts)] as const,
  );
}

const SELFTEST_TEXT = '__E2E_TOAST_DETECTOR_SELFTEST__';

/**
 * ⭐ 正对照：证明 toast 检测器真的能测到「出现」。
 * 判「某提示没有出现」之前必须先跑这个 —— 否则检测器恒空时用例无论功能好坏都通过（空洞通过）。
 * 自检后把假记录清掉，避免污染真实计数。
 */
export async function assertToastDetectorHealthy(page: Page): Promise<void> {
  const before = await readToasts(page);
  await page.evaluate(
    ([sel, text]) => {
      const el = document.createElement('div');
      el.className = `${sel} e2e-toast-detector-selftest`;
      el.textContent = text;
      document.body.appendChild(el);
    },
    [cfg().toastSelector.replace(/^\./, ''), SELFTEST_TEXT] as const,
  );
  await expect
    .poll(async () => (await readToasts(page)).some((t) => t.text === SELFTEST_TEXT), {
      timeout: 5000,
      message: `toast 检测器（选择器 ${cfg().toastSelector}）测不到注入的节点 ⇒ 检测器失效，本用例结论无效（不要改断言，先修量具）`,
    })
    .toBe(true);
  await page.evaluate(() => {
    document.querySelectorAll('.e2e-toast-detector-selftest').forEach((n) => n.remove());
  });
  await writeToasts(page, before);
  const after = await readToasts(page);
  expect(
    after.filter((t) => t.text === SELFTEST_TEXT),
    '自检记录必须被清干净',
  ).toHaveLength(0);
  expect(after).toEqual(before);
}

export function matchingToasts(toasts: ToastHit[]): ToastHit[] {
  return toasts.filter((t) => cfg().toastTextPattern.test(t.text));
}

// ---------------------------------------------------------------- 页面动作

export async function login(page: Page): Promise<void> {
  const c = cfg();
  expect(c.username, '必须设置 E2E_USERNAME').toBeTruthy();
  expect(c.password, '必须设置 E2E_PASSWORD').toBeTruthy();

  await page.goto('/login');
  const password = page.locator('input[type="password"]');
  await password.waitFor({ state: 'visible', timeout: 20000 });

  const tenantInput = page
    .locator('[data-testid="login-tenant"], input[name="tenantId"], input[name="tenant_id"]')
    .first();
  const usernameInput = page.locator('[data-testid="login-username"], input[name="username"]').first();

  if ((await tenantInput.count()) > 0 && (await usernameInput.count()) > 0) {
    await tenantInput.fill(c.tenant);
    await usernameInput.fill(c.username);
  } else {
    // 回退：登录表单的 text input 按顺序 = tenantId / username（见 memory 的既有配方）
    const texts = page.locator('input[type="text"]');
    const n = await texts.count();
    expect(
      n,
      '登录表单的 text input 少于 2 个，且没有可用的 testid/name ⇒ 无法定位租户与用户名',
    ).toBeGreaterThanOrEqual(2);
    await texts.nth(0).fill(c.tenant);
    await texts.nth(1).fill(c.username);
  }

  await password.fill(c.password);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 30000 });

  const s = await readStorage(page);
  expect(s.accessToken, '登录后 localStorage 没有 access token').toBeTruthy();
}

/**
 * 经 UI 切换到目标租户（走产品代码 switchTenant：impersonate → 备份 → 写 token → reload）。
 * ⛔ 不要用「直接调后端 + 自己写 localStorage」代替 —— 那会绕过被测的 switchTenant 逻辑。
 */
export async function switchTenantViaUi(page: Page, targetTenantId: string): Promise<void> {
  const trigger = await requireVisible(switcherTrigger(page), '「切换租户」触发器');
  await trigger.click();

  const option = await requireVisible(tenantOption(page, targetTenantId), `目标租户选项（${targetTenantId}）`);
  // 先登记导航监听再点击：switchTenant 末尾是 window.location.reload()
  const navigated = page.waitForEvent('framenavigated', {
    predicate: (f) => f === page.mainFrame(),
    timeout: 40000,
  });
  await option.click();
  await navigated;
  await page.waitForLoadState('load');
}

/**
 * 「切换租户」触发器（antd `Button`，testid 落在其 `<button>` 上）。
 * ⛔ 只认 testid，**没有回退链**：
 *  - 不用按钮文案 —— antd v6 会给两字按钮名插空格 ⇒ 必然假 FAIL；
 *  - 不用 `data-menu-id` / `role=button` + 文案 —— antd 内部实现细节，升级即变。
 */
function switcherTrigger(page: Page): Locator {
  return page.locator(`[data-testid="${TESTID.switcher}"]`);
}

/**
 * 目标租户菜单项（antd `Dropdown` 的 `menu.items` 配置式，testid 透传到其 `<li>`）。
 * 同上：只认 testid，不用 `data-menu-id`、不用 menuitem 文案。
 */
function tenantOption(page: Page, tenantId: string): Locator {
  return page.locator(`[data-testid="${TESTID.switcherOption(tenantId)}"]`);
}

/**
 * 解析单个被测元素；缺失（或等不到它可见）⇒ **显式 FAIL**，绝不静默跳过。
 * ⚠️ 用 `waitFor` 而不是 `count()` 快照：否则「还没渲染出来」会被误报成「缺 testid」。
 * 超时时长与 playwright.config.ts 的 `expect.timeout` 对齐。
 */
async function requireVisible(locator: Locator, what: string): Promise<Locator> {
  const target = locator.first();
  try {
    await target.waitFor({ state: 'visible', timeout: 15_000 });
  } catch {
    throw new Error(`定位不到${what}。\n${MISSING_TESTID_HINT}`);
  }
  return target;
}

/** 切换后必跑的哨兵：确认真的进入了模拟态，否则「没发 refresh」之类的断言会因为「压根没切」而空洞通过 */
export async function expectImpersonationActive(page: Page, targetTenantId: string): Promise<void> {
  await expect.poll(async () => (await readStorage(page)).accessToken, { timeout: 20000 }).toBeTruthy();
  await expectTokenTenant(page, targetTenantId, true, '模拟态');
  const s = await readStorage(page);
  expect(s.user, '模拟态应有 jonex_user 缓存').toBeTruthy();
  const user = JSON.parse(s.user as string) as { impersonated?: boolean; tenantId?: string };
  expect(user.impersonated, 'jonex_user.impersonated 应为 true（否则横幅不渲染、S2 会连带失败）').toBe(true);
  expect(user.tenantId, 'jonex_user.tenantId 应为目标租户').toBe(targetTenantId);
}

export async function expectBannerVisible(page: Page): Promise<void> {
  // ⛔ 只认 testid，**不**回退 `role=alert`：横幅虽然自带该 role，但页面上别的 alert/notification
  //    也会命中 ⇒ 回退会让「横幅没渲染」这类缺陷变成假绿。
  const banner = await requireVisible(page.locator(`[data-testid="${TESTID.banner}"]`), '模拟态横幅');
  await expect(banner).toBeVisible();
}

/** 模拟 token 的剩余有效秒数（负数 = 已过期）。以 payload.exp 为准（契约 §3.4） */
export function remainingSeconds(payload: Record<string, unknown>): number {
  const exp = Number(payload.exp);
  expect(Number.isFinite(exp), 'access token payload 缺 exp ⇒ 无法计算到期时点').toBe(true);
  return exp - Math.floor(Date.now() / 1000);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 把 localStorage 的到期时间戳改成「已过期」，制造「临近/已过期」的确定性前置（见各 spec 的说明） */
export async function seedStaleExpiresAt(page: Page, agoMs = 60_000): Promise<void> {
  await page.evaluate(([key, value]) => localStorage.setItem(key as string, value as string), [
    STORAGE.expiresAt,
    String(Date.now() - agoMs),
  ] as const);
}

/**
 * 只点「切换租户 → 目标租户」，**不等 reload**。
 * 用于 S9：那里 switchTenant 内部的 fetchCurrentUser() 被拦成 401 ⇒ 末尾的 reload 不会发生，
 * 用 switchTenantViaUi 会干等到超时并掩盖真正的缺陷。
 */
export async function selectTenantOption(page: Page, targetTenantId: string): Promise<void> {
  const trigger = await requireVisible(switcherTrigger(page), '「切换租户」触发器');
  await trigger.click();
  const option = await requireVisible(tenantOption(page, targetTenantId), `目标租户选项（${targetTenantId}）`);
  await option.click();
}

/** 等待「自动到期退出模拟」落地：access token 不再是模拟 token */
export async function waitForAutoExit(page: Page, timeoutMs = 90_000): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await readStorage(page);
        if (!s.accessToken) return 'no-token';
        return decodeJwtPayload(s.accessToken)?.impersonated === true ? 'impersonating' : 'exited';
      },
      { timeout: timeoutMs, message: '到期后没有自动退出模拟（access token 仍为模拟 token）' },
    )
    .toBe('exited');
}

export interface ResponseHit {
  url: string;
  status: number;
  body: string;
}

/** 采集 /api/v1/auth/refresh 的响应（状态码 + 响应体），供 S7 断言 200 与 access_token */
export function recordRefreshResponses(page: Page): ResponseHit[] {
  const hits: ResponseHit[] = [];
  page.on('response', async (res) => {
    if (!res.url().includes(REFRESH_PATH)) return;
    let body = '';
    try {
      body = await res.text();
    } catch {
      /* 响应体读不到（被中断等）：保留空串，由断言侧给出可读失败信息 */
    }
    hits.push({ url: res.url(), status: res.status(), body });
  });
  return hits;
}
