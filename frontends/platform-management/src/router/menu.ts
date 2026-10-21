import type { ComponentType } from 'react';
import {
  HomeOutlined,
  ApiOutlined,
  TeamOutlined,
  UserOutlined,
  SafetyOutlined,
  ScheduleOutlined,
  SettingOutlined,
  FileTextOutlined,
  CloudServerOutlined,
  CodeOutlined,
  BarChartOutlined,
  DatabaseOutlined,
  MonitorOutlined,
} from '@ant-design/icons';

/** 侧边栏菜单项 */
export interface MenuItem {
  key: string;
  path?: string;
  icon?: string;
  label: string;
  permissionCode?: string | string[];
  children?: MenuItem[];
}

/** 路由上的菜单元数据（方案二：路由驱动菜单） */
export interface RouteMenuMeta {
  icon?: string;
  order?: number;
  permissionCode?: string | string[];
  hidden?: boolean;
}

/**
 * 菜单 / 路由可见性判定：`permissionCode` 可以是单个码，也可以是**码数组（任意一个命中即可）**。
 *
 * 数组语义是合并「系统配置」页时引入的：该页对平台管理员和租户管理员都可见，但两人持的码不同
 * （`platform:config:read` / `tenant:security:read`），页面内部再按码分段渲染。
 *
 * ⚠ 空数组等于「谁都看不见」（`some` 为空为 false）——这是刻意的：写成 `[]` 是配置错误，
 *   宁可不显示，也不要无码放行。
 */
export function hasMenuPermission(permissionCode: string | string[] | undefined, permissions: string[]): boolean {
  if (!permissionCode) return true;
  const codes = Array.isArray(permissionCode) ? permissionCode : [permissionCode];
  return codes.some((code) => permissions.includes(code));
}

/** 图标注册表：icon 字符串 → 组件 */
export const IconMap: Record<string, ComponentType> = {
  HomeOutlined,
  ApiOutlined,
  TeamOutlined,
  UserOutlined,
  SafetyOutlined,
  ScheduleOutlined,
  SettingOutlined,
  FileTextOutlined,
  CloudServerOutlined,
  CodeOutlined,
  BarChartOutlined,
  DatabaseOutlined,
  MonitorOutlined,
};

interface MenuRoute {
  path?: string;
  title?: string;
  menu?: RouteMenuMeta;
}

/**
 * 从路由配置生成侧边栏菜单（方案二）。
 * 取 Layout 的 children 中带 menu 元数据、且未隐藏的路由，按 order 排序。
 * @param routes getRoutes() 返回值（顶层含 children 的布局路由）
 * @param t i18n 翻译函数（title 存的是 key）
 */
export function getMenuFromRoutes(routes: Array<{ children?: MenuRoute[] }>, t: (key: string) => string): MenuItem[] {
  const layout = routes.find((r) => Array.isArray(r.children) && r.children.length > 0);
  const children = layout?.children ?? [];

  return children
    .filter((r) => r.menu && !r.menu.hidden)
    .sort((a, b) => (a.menu?.order ?? 0) - (b.menu?.order ?? 0))
    .map((r) => ({
      key: r.path || '',
      path: `/${r.path}`,
      icon: r.menu?.icon,
      label: r.title ? t(r.title) : r.path || '',
      permissionCode: r.menu?.permissionCode,
    }));
}
