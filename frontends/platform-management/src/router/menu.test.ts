/**
 * 菜单 / 路由权限判定 —— `hasMenuPermission` 的「任一命中」语义。
 *
 * 为什么值得单独测：合并「系统配置」页时引入了「一个路由对两套码可见」
 * （`platform:config:read` **或** `tenant:security:read`）。这种判定写错的后果是**静默的** ——
 * 要么租户管理员看不到自己唯一的安全页（菜单被裁掉 / 守卫 redirect 403），
 * 要么菜单看得见、点进去 403（菜单与守卫两份实现不一致）。
 * 这两条都不会报错，只会让人「莫名其妙进不去」。
 */
import { describe, expect, it } from 'vitest';
import { getMenuFromRoutes, hasMenuPermission } from './menu';

describe('hasMenuPermission', () => {
  it('没配码的菜单对所有人可见', () => {
    expect(hasMenuPermission(undefined, [])).toBe(true);
    expect(hasMenuPermission(undefined, ['any:code'])).toBe(true);
  });

  it('单码：持有才可见', () => {
    expect(hasMenuPermission('audit:read', ['audit:read'])).toBe(true);
    expect(hasMenuPermission('audit:read', ['other:code'])).toBe(false);
    expect(hasMenuPermission('audit:read', [])).toBe(false);
  });

  it('码数组：任一命中即可（合并页的关键语义）', () => {
    const either = ['platform:config:read', 'tenant:security:read'];
    // 租户管理员（只有租户码）与平台管理员（两个都有）都要看得见
    expect(hasMenuPermission(either, ['tenant:security:read'])).toBe(true);
    expect(hasMenuPermission(either, ['platform:config:read'])).toBe(true);
    expect(hasMenuPermission(either, ['platform:config:read', 'tenant:security:read'])).toBe(true);
    // 不相关的码不能放行
    expect(hasMenuPermission(either, ['unrelated:code'])).toBe(false);
    expect(hasMenuPermission(either, [])).toBe(false);
  });

  it('空数组 = 谁都看不见（配置错误宁可不显示，也不要无码放行）', () => {
    expect(hasMenuPermission([], ['platform:config:read'])).toBe(false);
    expect(hasMenuPermission([], [])).toBe(false);
  });
});

describe('getMenuFromRoutes 透传码数组', () => {
  const routes = [
    {
      children: [
        {
          path: 'system-config',
          title: 'navigation.systemConfig',
          menu: { order: 9, permissionCode: ['platform:config:read', 'tenant:security:read'] },
        },
        { path: 'no-code-page', title: 'navigation.home', menu: { order: 1 } },
      ],
    },
  ];
  const t = (key: string) => key;

  it('数组原样透传给菜单项，供过滤方使用', () => {
    const items = getMenuFromRoutes(routes, t);
    expect(items.find((i) => i.path === '/system-config')?.permissionCode).toEqual([
      'platform:config:read',
      'tenant:security:read',
    ]);
  });

  it('菜单过滤：任一命中即保留，全不命中才裁掉（按 order 排序）', () => {
    const items = getMenuFromRoutes(routes, t);
    const visiblePaths = (perms: string[]) =>
      items.filter((i) => hasMenuPermission(i.permissionCode, perms)).map((i) => i.path);

    expect(visiblePaths(['tenant:security:read'])).toEqual(['/no-code-page', '/system-config']);
    expect(visiblePaths(['platform:config:read'])).toEqual(['/no-code-page', '/system-config']);
    expect(visiblePaths(['unrelated:code'])).toEqual(['/no-code-page']);
  });
});
