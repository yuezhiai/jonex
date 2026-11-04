/**
 * S9 模拟态 401 自动还原原租户、不触发登出（异常流程 — 会话）—— 最高优先级
 * 契约：docs/auth/tenant-impersonation-refresh-isolation-plan.md §3.2 硬不变量 2/3、§3.4、§4 S9
 *
 * 验收语义（2026-10-20 确认：改断言验收产品现行语义，不改产品逻辑）：
 *
 * Given 管理员已切换到 tenant_jonex_sso
 * When  业务请求返回 401
 * Then  不发出 jonex:token-expired 事件
 * And   发出 jonex:impersonation-expired 事件（401 改道还原原租户）
 * And   自动退出模拟：localStorage 的 access token 还原为原租户、不再是模拟 token
 * And   备份 jonex_impersonation_backup 被还原流程**正常消费**（还原后删除，不是原封不动留着）
 * And   jonex_user 缓存回到原租户（不再是目标租户）
 * And   不跳转到登录页
 * And   整页重载后后续请求的 Bearer 属于原租户、且不带模拟标记
 *
 * 【为什么不是「备份仍存在」】—— 旧断言来自缺陷修复前的设计意图，已与产品语义冲突：
 * 模拟态 401 的正确收尾是「还原原租户 + 重载」（§3.2 硬不变量 2），而 `restoreAuthFromImpersonation()`
 * 在还原时会删掉备份（备份是「一次性凭证」，还原即消费）。备份**在模拟态期间**绝不可被清除
 * （若被 401 登出分支的 clearAuthStorage 删掉，用户就回不到原租户，再也退不出模拟）。
 *
 * 【本用例的 401 是怎么造出来的】—— 用真实路径，不用假造噪声
 * `switchTenant()` 内部就是 `impersonate() → 备份 → 写模拟 token → await fetchCurrentUser() → reload`，
 * 而 `fetchCurrentUser()` 打的是 axios 客户端上的 `/auth/me`。把**模拟态下**的 `/auth/me`
 * 拦成 401，就等于「模拟态下的一次业务请求 401」，且必然发生（反空洞断言会证明它真的发生了）。
 *
 * ⛔ 401 只拦「带模拟 token 的请求」：还原后的 `/auth/me`（含整页重载后的启动校验）必须放行，
 * 否则等于在测「一个永远坏掉的 /auth/me」，会把正常的会话踢回登录页，测不出真实语义。
 */
import { expect, test } from '@playwright/test';
import {
  EVENTS,
  cfg,
  decodeJwtPayload,
  expectTokenTenant,
  installEventRecorder,
  login,
  navigatedToLogin,
  readEvents,
  readStorage,
  recordNavigations,
  recordNetwork,
  selectTenantOption,
  sleep,
} from '../lib/harness';

test.describe('S9 模拟态 401 自动还原原租户', () => {
  test('模拟态业务 401：不发 token-expired、自动还原原租户、备份被消费、不跳登录页', async ({ page }) => {
    const c = cfg();
    test.setTimeout(180_000);

    // ---------- Arrange ----------
    await login(page);
    const net = recordNetwork(page);
    const navs = recordNavigations(page);
    await installEventRecorder(page, [EVENTS.tokenExpired, EVENTS.impersonationExpired]);
    await expectTokenTenant(page, c.tenant, false, '登录态');

    // 反空洞证据：记录「被强制 401 的那次请求」自己携带的 token 载荷。
    // 必须证明 401 发生时确实处于**目标租户的模拟态** —— 否则下面的「没登出」毫无意义。
    const forced401Payloads: (Record<string, unknown> | null)[] = [];

    // 把**模拟态下**的业务接口拦成 401；还原后的请求一律放行（见文件头 ⛔）
    await page.route(c.probe401Pattern, (route) => {
      const auth = route.request().headers()['authorization'] || '';
      const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
      const payload = decodeJwtPayload(token);
      if (payload?.impersonated === true) {
        forced401Payloads.push(payload);
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            success: false,
            code: 1002,
            message: 'E2E forced 401 (probe)',
          }),
        });
      }
      // 非模拟态（登录态 / 还原后的原租户请求）：真实放行，不制造噪声
      return route.continue();
    });

    // ---------- Act ----------
    // switchTenant 里 fetchCurrentUser() 会 401 抛错、reload 不执行；401 拦截器改道
    // impersonation-expired → AppShellLayout 调 autoExitImpersonation() → 还原 → 整页重载。
    // ⛔ 等的是 `load`（整页重载）而**不是** `framenavigated` —— 后者对 SPA 的 pushState
    //    同文档导航也会触发，可能提前 resolve 到未还原的状态（假绿/抖动）。
    const autoExitReload = page.waitForEvent('load', { timeout: 90_000 });
    await selectTenantOption(page, c.targetTenant);
    await autoExitReload;
    await page.waitForLoadState('load');

    // ⭐ 反空洞一：401 真的发生了。
    expect(
      forced401Payloads.length,
      `401 探针（${c.probe401Pattern}）没有被触发 ⇒ 本用例没有制造出 401，结论无效`,
    ).toBeGreaterThan(0);
    // ⭐ 反空洞二：401 发生时确实处于目标租户的模拟态（缺陷路径只在这个前提下成立）
    expect(
      forced401Payloads[0]?.tenant_id,
      `被强制 401 的请求应带目标租户 token；实际=${JSON.stringify(forced401Payloads[0])}`,
    ).toBe(c.targetTenant);
    expect(
      forced401Payloads[0]?.impersonated,
      `被强制 401 的请求应是模拟 token；实际=${JSON.stringify(forced401Payloads[0])}`,
    ).toBe(true);

    // 给重载后的启动请求（/auth/me 权威校验、菜单等）一点落地时间再读事件
    await sleep(1500);

    // ---------- 主断言：自动还原原租户 ----------
    await expectTokenTenant(page, c.tenant, false, '401 自动退出后');

    const s = await readStorage(page);
    expect(s.backup, '备份应被还原流程正常消费（restoreAuthFromImpersonation 还原后删除）').toBeNull();
    expect(s.user, '还原后 jonex_user 缓存应存在').toBeTruthy();
    const cachedUser = JSON.parse(s.user as string) as { tenantId?: string; impersonated?: boolean };
    expect(cachedUser.tenantId, 'jonex_user 缓存应回到原租户').toBe(c.tenant);
    expect(!!cachedUser.impersonated, 'jonex_user 缓存不得残留模拟标记').toBe(false);

    const events = await readEvents(page);
    expect(
      events,
      `模拟态 401 不得发出 ${EVENTS.tokenExpired}（那会走 clearAuthStorage → 删掉回原租户的唯一备份）`,
    ).not.toContain(EVENTS.tokenExpired);
    expect(
      events,
      `模拟态 401 应改发 ${EVENTS.impersonationExpired}（还原原租户的信号；没发说明 401 没走到改道分支）`,
    ).toContain(EVENTS.impersonationExpired);

    expect(navigatedToLogin(navs), `模拟态 401 不得跳登录页；跳转链=${JSON.stringify(navs)}`).toBe(false);

    // ---------- 重载后的后续请求：Bearer 属于原租户且非模拟态 ----------
    const bearerBefore = net.bearer.length;
    await page.reload();
    await page.waitForLoadState('load');
    await expect
      .poll(() => net.bearer.length, {
        timeout: 20_000,
        message: '还原后应用没有发出任何带 Bearer 的请求 ⇒ 无法验证「后续 Bearer 属原租户」（反空洞失败）',
      })
      .toBeGreaterThan(bearerBefore);

    const after = net.bearer.slice(bearerBefore);
    const decoded = after.map((b) => decodeJwtPayload(b.token)).filter(Boolean) as Record<string, unknown>[];
    expect(decoded.length, '还原后的 Bearer 都应可解码').toBeGreaterThan(0);
    expect(
      decoded.every((p) => p.tenant_id === c.tenant),
      `还原后的请求必须带原租户 token；实际=${JSON.stringify(decoded.map((p) => p.tenant_id))}`,
    ).toBe(true);
    expect(
      decoded.some((p) => p.impersonated === true),
      '还原后的请求不得再带模拟态 token',
    ).toBe(false);

    // ---------- ⭐ 正对照（判「未出现」必须配） ----------
    // 先证明「事件 → 跳登录页」这条链本身是通的：若应用真的发过事件，我们必然会看到跳转。
    // ⚠️ 必须等跳转落地再读事件：派发后应用同步执行 window.location.href，
    //    导航会销毁文档上下文，抢先 evaluate 会报 "Execution context was destroyed"
    //    （2026-10-26 实测踩坑）。navigations 记录在 Node 侧，不受文档销毁影响。
    await page
      .evaluate((ev) => {
        window.dispatchEvent(new CustomEvent(ev));
      }, EVENTS.tokenExpired)
      .catch(() => {
        /* 导航已经开始时 evaluate 可能被中断，跳转本身由下面的 poll 判定 */
      });
    await expect
      .poll(() => navigatedToLogin(navs), {
        timeout: 20_000,
        message: `正对照失败：派发 ${EVENTS.tokenExpired} 后没有跳登录页 ⇒ 跳转链不成立，上面「不跳登录页」的断言无判别力；跳转链=${JSON.stringify(
          navs,
        )}`,
      })
      .toBe(true);
    await page.waitForLoadState('load').catch(() => {
      /* 登录页可能仍在加载，下面的 poll 会给出最终判定 */
    });

    // 再证明「事件名 + 采集器」有效：跳转后读 sessionStorage（同源导航保留），采集器必须听得到。
    await expect
      .poll(async () => (await readEvents(page)).includes(EVENTS.tokenExpired), {
        timeout: 10_000,
        message: `正对照失败：手工派发的 ${EVENTS.tokenExpired} 没被采集到 ⇒ 「没发事件」的判定无判别力`,
      })
      .toBe(true);

    test.info().annotations.push({
      type: 'observed',
      description: `forced401=${forced401Payloads.length} / bearer(after exit)=${decoded.length} / navs=${JSON.stringify(navs)}`,
    });
  });
});
