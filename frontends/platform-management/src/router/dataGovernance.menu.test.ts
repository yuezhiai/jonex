/**
 * 资产目录（第 2 期数据治理）菜单/路由注册。
 *
 * standalone 菜单由 routes.config.ts 的路由元数据驱动，hosted 菜单由后端
 * platform.menus 驱动 —— 两处必须同路径、同文案 key、同权限码，否则会出现
 * 「hosted 有入口、standalone 没有」（或反之）。本测试守住 standalone 这一半。
 */
import { describe, expect, it } from 'vitest';
import { getRoutes } from './routes.config';
import { getMenuFromRoutes, IconMap } from './menu';

describe('资产目录菜单注册', () => {
  const t = (key: string) => key;

  it('路由/菜单存在、路径扁平、权限码为 knowledge:read', () => {
    const items = getMenuFromRoutes(getRoutes('standalone', t) as any, t);
    const item = items.find((i) => i.path === '/data-governance/assets');
    expect(item).toBeTruthy();
    expect(item?.label).toBe('navigation.dataGovernance');
    expect(item?.permissionCode).toBe('knowledge:read');
    expect(item?.icon).toBe('DatabaseOutlined');
  });

  it('排在数据运营与平台监控之后', () => {
    const items = getMenuFromRoutes(getRoutes('standalone', t) as any, t);
    const paths = items.map((i) => i.path);
    // 平台监控（order 4）插在两者之间，资产目录仍是「数据运营之后」的第二项
    expect(paths.indexOf('/platform-monitor')).toBe(paths.indexOf('/data-operations') + 1);
    expect(paths.indexOf('/data-governance/assets')).toBe(paths.indexOf('/platform-monitor') + 1);
  });

  it('菜单 order 两两不重复（防新旧项顺延漂移）', () => {
    const routes = getRoutes('standalone', t) as any[];
    const layout = routes.find((r) => Array.isArray(r.children) && r.children.length > 0);
    const orders = layout.children
      .filter((r: any) => r.menu)
      .map((r: any) => r.menu.order);
    expect(orders.every((order: unknown) => typeof order === 'number')).toBe(true);
    expect(new Set(orders).size).toBe(orders.length);
  });

  it('图标已在 IconMap 注册', () => {
    expect(IconMap.DatabaseOutlined).toBeTruthy();
    expect(IconMap.MonitorOutlined).toBeTruthy();
  });
});
