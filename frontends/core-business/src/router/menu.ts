import type { ComponentType } from 'react';
import { SearchOutlined, BlockOutlined, DatabaseOutlined, ClusterOutlined } from '@ant-design/icons';

/** 侧边栏菜单项 */
export interface MenuItem {
  key: string;
  path?: string;
  icon?: string;
  label: string;
  permissionCode?: string;
  children?: MenuItem[];
}

/** 路由上的菜单元数据（方案二：路由驱动菜单） */
export interface RouteMenuMeta {
  icon?: string;
  order?: number;
  permissionCode?: string;
  hidden?: boolean;
}

/** 图标注册表：icon 字符串 → 组件 */
export const IconMap: Record<string, ComponentType> = {
  SearchOutlined,
  BlockOutlined,
  DatabaseOutlined,
  ClusterOutlined,
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
