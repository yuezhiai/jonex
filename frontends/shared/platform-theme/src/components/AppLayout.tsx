import React, { Fragment, useEffect, useMemo, useState } from 'react';
import { Button, Dropdown, Layout, Menu, Popover, Spin, Tooltip } from 'antd';
import type { MenuProps } from 'antd';
import {
  CaretDownOutlined,
  GlobalOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';

/** 语言切换仅支持 zh/en；选项显示名按当前界面语言取值（封闭 2 语言矩阵，值与原 i18n-resources
 *  language.zh/en 键一致）。纯 UI 包不引入 i18n 运行时，文案内联于此，zh/en.json 对应键已移除 */
const LANGUAGE_VALUES = ['zh', 'en'] as const;
const LANGUAGE_NAMES: Record<string, { zh: string; en: string }> = {
  zh: { zh: '中文', en: 'English' },
  en: { zh: 'Simplified Chinese', en: 'English' },
};

/** 语言项展示名：table 随当前界面语言（current）切换；未知 locale 回落 en，未知 value 回显原值 */
const localeName = (current: string | undefined, value: string): string => {
  const table = LANGUAGE_NAMES[current ?? 'en'] ?? LANGUAGE_NAMES.en;
  return table[value as keyof typeof table] ?? value;
};

/** slot 求值上下文：折叠状态与切换器 */
export interface AppLayoutCtx {
  collapsed: boolean;
  toggle: () => void;
}

/** 壳注入槽：直接给元素，或给接收 collapsed 的渲染函数 */
type Slot = React.ReactNode | ((ctx: AppLayoutCtx) => React.ReactNode);

const resolveSlot = (slot: Slot, ctx: AppLayoutCtx): React.ReactNode =>
  typeof slot === 'function' ? (slot as (c: AppLayoutCtx) => React.ReactNode)(ctx) : slot;

/** 顶栏 logo 数据：组件在 header 最左统一渲染 <a><img>，资源随 locale 切换，SPA 跳转走 onNavigate */
export interface LayoutLogo {
  /** 点击跳转目标（组件不依赖 react-router，经 onNavigate 回调导航） */
  href: string;
  /** img alt 与可访问性标签 */
  alt: string;
  /** 当前语言：'en' 时显示 logo-en 资源，其余显示默认 logo；不随侧栏折叠变化 */
  locale?: string;
  /** 传了就用这段文字替代 logo 图片（演示期临时换品牌名用；不传保持原图） */
  text?: string;
}

/** 侧栏导航树节点：label 已由壳翻译、icon 已由壳解析为 ReactNode，组件零 i18n / 图标注册表依赖 */
export interface LayoutNavItem {
  key: string;
  label: React.ReactNode;
  icon?: React.ReactNode;
  /** 叶子导航目标（SPA 跳转走 onNavigate）；无 path 的节点视为纯分组 */
  path?: string;
  /** 有 children 即分组：顶层为静态 section（仅标题），分组内可再含分组或叶子 */
  children?: LayoutNavItem[];
}

/** 用户下拉菜单动作项 */
export interface LayoutUserAction {
  key: string;
  icon?: React.ReactNode;
  label: React.ReactNode;
  onClick?: () => void;
}

/** 顶栏用户区数据：组件渲染首字母头像（必要时带姓名/角色文本）+ actions 下拉 */
export interface LayoutUser {
  avatarText: string;
  /** 提供时显示在头像右侧（展开态），如姓名 */
  name?: React.ReactNode;
  /** 提供时显示在姓名下的小字，如角色 */
  subtitle?: React.ReactNode;
  actions?: LayoutUserAction[];
}

/** 顶栏语言切换受控数据：选项表为固定常量内置组件，仅当前语言与切换副作用随壳变化 */
export interface LayoutLanguage {
  /** 当前语言值（与内置选项 value 对齐，如 'zh' / 'en'） */
  current?: string;
  /** 切换副作用：切 i18n 语言 + 持久化 + 广播（shell/iframe 子应用联动由壳承担） */
  onSwitch: (value: string) => void;
}

/** 顶栏右侧：固定项（语言/用户）+ 壳特有 custom 入口。渲染顺序 custom → language → user */
export interface LayoutTopbarRight {
  /** 壳特有入口（shell 的租户模拟切换等），支持函数形式感知折叠态 */
  custom?: Slot;
  language?: LayoutLanguage;
  user?: LayoutUser;
}

export interface AppLayoutProps {
  /** 顶栏 logo：渲染在 header 最左，按 locale 切 logo / logo-en，由组件处理 */
  logo: LayoutLogo;
  /** 侧栏导航树：展开态渲染 section/分组/叶子，折叠态渲染悬浮 Tooltip/Popover，均组件内置 */
  navItems: LayoutNavItem[];
  /** 导航顶部常驻块（如空间切换器），可选函数形式感知折叠态；引擎用 `.yx-sidebar-slot` sticky 包裹 */
  sidebarExtra?: Slot;
  /** 导航数据异步加载中（shell 拉取后端菜单），组件显示居中小加载态 */
  navLoading?: boolean;
  /** 当前路由路径（组件内不依赖 react-router），用于激活项高亮与分组默认展开 */
  activePath: string;
  /** logo 点击 + 导航点击统一的 SPA 跳转回调 */
  onNavigate: (path: string) => void;
  /** 是否渲染侧栏折叠钮（shell=true；standalone 省略=false 时不渲染，侧栏不可折叠）。
   *  按钮骑在侧栏右边界、垂直居中，展开态与折叠态均可见 */
  showCollapseTrigger?: boolean;
  /** 顶栏右侧受控固定项；间距统一：展开 20 / 折叠 8，由 CSS 容器控制 */
  topbarRight?: LayoutTopbarRight;
  /** `.yx-content` 内容；需要通栏警示的壳（shell 的 impersonation Alert）放其顶部自渲染 */
  children?: React.ReactNode;
}

/** 当前路由是否命中菜单项 path（含子路由）。导出给宿主复用，避免同口径出现第二份实现 */
export const isPathActive = (path: string | undefined, activePath: string): boolean =>
  !!path && (activePath === path || activePath.startsWith(path + '/'));

/** 子树内是否存在命中 activePath 的叶子节点 */
const subtreeActive = (item: LayoutNavItem, activePath: string): boolean => {
  if (item.children?.length) return item.children.some((child) => subtreeActive(child, activePath));
  return isPathActive(item.path, activePath);
};

/** 收集 activePath 所在子树经过的全部分组 key（用于默认展开） */
const collectActiveGroupKeys = (items: LayoutNavItem[], activePath: string): string[] => {
  const keys: string[] = [];
  for (const item of items) {
    if (item.children?.length) {
      if (subtreeActive(item, activePath)) keys.push(item.key);
      keys.push(...collectActiveGroupKeys(item.children, activePath));
    }
  }
  return keys;
};

/** 子树内命中 activePath 的叶子 key（用于折叠态悬浮菜单高亮当前项） */
const collectActiveLeafKeys = (items: LayoutNavItem[], activePath: string): string[] =>
  items.flatMap((item) =>
    item.children?.length
      ? collectActiveLeafKeys(item.children, activePath)
      : isPathActive(item.path, activePath)
        ? [item.key]
        : [],
  );

/** 侧栏宽度：antd Sider 以 JS 数值（而非 CSS 变量）驱动 width/flex-basis 内联样式，故宽度单一来源在此。
 *  折叠宽度同时决定窄屏断点下的形态（breakpoint="md" → 767.98px）。 */
const SIDEBAR_WIDTH = 200;
/** 折叠态宽度取 64：窄栏内导航图标是水平居中的（.yx-nav-collapsed-icon 宽 100%），
 *  而折叠钮骑在窄栏右边界上（.yx-sider-toggle 绝对定位 right:0 + translateX(50%)，
 *  自身宽度与侧栏无关），故收缩宽度只拉近图标与钮的横向间距 ——
 *  64px 下两者仍留约 12px 余量，再窄就会糊成一团。 */
const SIDEBAR_COLLAPSED_WIDTH = 64;

/** 四壳统一布局骨架：通栏 header（含 logo）+ 下方 sidebar/content 分栏，折叠状态机 + 通用导航/顶栏项。
 *  布局原语由 antd Layout 组件族承担（Layout / Header / Sider / Content），CSS 只做主题化覆盖。
 *  不依赖 react-router / i18n / store —— 数据均为受控输入（label 已翻译、图标已解析、动作由壳回调）。 */
export default function AppLayout({
  logo,
  navItems,
  sidebarExtra,
  navLoading = false,
  activePath,
  onNavigate,
  showCollapseTrigger = false,
  topbarRight,
  children,
}: AppLayoutProps) {
  const [collapsed, setCollapsed] = useState(false);
  // 展开态分组开合（跨折叠切换持久）；折叠态单个悬浮入口 key
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [collapsedOpenKey, setCollapsedOpenKey] = useState<string | null>(null);

  // 窄屏折叠由 antd Sider 的 breakpoint="md"（767.98px）+ onBreakpoint 驱动，
  // 挂载时即回调一次，且在窗口缩放时实时切换 —— 无需自行监听 resize。

  // 默认展开当前路由所在组：菜单首次加载即初始化；后续只合并不收回手动状态。
  // navItems 由各壳每次渲染派生（引用不稳定），effect 需在无新增分组时返回原引用避免循环 setState
  useEffect(() => {
    if (!navItems.length) return;
    const keys = collectActiveGroupKeys(navItems, activePath);
    if (!keys.length) return;
    setExpandedKeys((prev) => {
      const missing = keys.filter((key) => !prev.includes(key));
      return missing.length ? [...prev, ...missing] : prev;
    });
  }, [activePath, navItems]);

  const ctx: AppLayoutCtx = useMemo(
    () => ({ collapsed, toggle: () => setCollapsed((prev) => !prev) }),
    [collapsed],
  );

  const toggleGroup = (key: string) => {
    setExpandedKeys((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };

  const lang = topbarRight?.language;
  const user = topbarRight?.user;

  // ── 展开态导航渲染 ──────────────────────────────────────────────
  /** 叶子（顶层直接项或分组内子项共用），点击导航 */
  const renderLeaf = (item: LayoutNavItem, variant: 'direct' | 'sub'): React.ReactNode => {
    const active = isPathActive(item.path, activePath);
    const inner = (
      <>
        {variant === 'direct' ? (
          item.icon ? (
            <span className="yx-nav-icon">{item.icon}</span>
          ) : (
            <span className="yx-sub-dot" />
          )
        ) : (
          <span className="yx-sub-dot" />
        )}
        <span>{item.label}</span>
      </>
    );
    if (variant === 'sub') {
      return (
        <div
          key={item.key}
          className={`yx-sub-item${active ? ' active' : ''}`}
          onClick={() => {
            if (item.path) onNavigate(item.path);
          }}
          style={{ cursor: 'pointer' }}
        >
          {inner}
        </div>
      );
    }
    return (
      <a
        key={item.key}
        className={`yx-nav-item${active ? ' active' : ''}`}
        onClick={(e) => {
          e.preventDefault();
          if (item.path) onNavigate(item.path);
        }}
        style={{ cursor: 'pointer', textDecoration: 'none' }}
      >
        {inner}
      </a>
    );
  };

  /** 可折叠分组：组头（图标 + 名称 + 箭头），展开态渲染子项/子分组 */
  const renderGroup = (item: LayoutNavItem): React.ReactNode => {
    const key = item.key;
    const expanded = expandedKeys.includes(key);
    const children = item.children ?? [];
    const hasActiveChild = children.some((child) => subtreeActive(child, activePath));

    return (
      <div key={key} className={`yx-nav-group${expanded ? ' open' : ''}`}>
        <div
          className={`yx-nav-group-header${hasActiveChild && !expanded ? ' active' : ''}`}
          onClick={() => toggleGroup(key)}
          style={{ cursor: 'pointer' }}
        >
          {item.icon ? <span className="yx-nav-icon">{item.icon}</span> : <span className="yx-sub-dot" />}
          <span>{item.label}</span>
          <RightOutlined className="yx-nav-arrow" />
        </div>
        {expanded && (
          <div className="yx-sub-menu">
            {children.map((child) => (child.children?.length ? renderGroup(child) : renderLeaf(child, 'sub')))}
          </div>
        )}
      </div>
    );
  };

  /** 顶层项：带 children 为静态 section（仅标题，不可折叠）；否则为直接导航项 */
  const renderTopItem = (item: LayoutNavItem): React.ReactNode => {
    const children = item.children ?? [];
    if (children.length) {
      return (
        <div key={item.key} style={{ marginBottom: 4 }}>
          <div className="yx-nav-section">{item.label}</div>
          {children.map((child) => (child.children?.length ? renderGroup(child) : renderLeaf(child, 'direct')))}
        </div>
      );
    }
    return renderLeaf(item, 'direct');
  };

  // ── 折叠态导航渲染：悬浮 Tooltip / Popover ─────────────────────
  /** 分组悬浮项：叶子可点导航；组中组递归为 submenu */
  const buildPopupLeafItems = (items: LayoutNavItem[]): NonNullable<MenuProps['items']> =>
    items.map((item) => {
      if (item.children?.length) {
        return {
          key: `sub-${item.key}`,
          label: item.label,
          children: buildPopupLeafItems(item.children),
        } as NonNullable<MenuProps['items']>[number];
      }
      return {
        key: String(item.key),
        label: <span>{item.label}</span>,
        onClick: () => {
          setCollapsedOpenKey(null);
          if (item.path) onNavigate(item.path);
        },
      } as NonNullable<MenuProps['items']>[number];
    });

  /** 折叠态单个入口：含子项 → hover/click 弹出分组菜单；叶子 → Tooltip + 点击导航 */
  const renderCollapsedEntry = (item: LayoutNavItem): React.ReactNode => {
    const glyph = item.icon ? <span className="yx-nav-icon">{item.icon}</span> : <span className="yx-sub-dot" />;
    const children = item.children ?? [];
    if (children.length) {
      const active = children.some((child) => subtreeActive(child, activePath));
      const popupMenu = (
        <div style={{ padding: 2 }}>
          <Menu
            className="yx-nav-popup-menu"
            // 必须显式传 false：Sider 的 context 会穿透 Popover —— portal 只换 DOM 挂载点、不改 React 树，
            // 折叠态下 Menu 读到 siderCollapsed=true，被 rc-menu 无条件打上 .ant-menu-inline-collapsed
            //（rc-menu 里 `[`-inline-collapsed`]: internalInlineCollapsed` 不看 mode），
            // antd 配套规则随即把 .ant-menu-title-content 置为 width:0/opacity:0 → 浮窗里文字整片消失。
            // antd 内部取值是 `inlineCollapsed ?? siderCollapsed`，故传 false 即可切断这条继承。
            inlineCollapsed={false}
            style={{ minWidth: 160, borderInlineEnd: 'none' }}
            items={[{ type: 'group' as const, label: item.label, children: buildPopupLeafItems(children) }]}
            selectedKeys={collectActiveLeafKeys(children, activePath)}
          />
        </div>
      );
      return (
        <Popover
          key={item.key}
          placement="rightTop"
          trigger={['hover', 'click']}
          content={popupMenu}
          open={collapsedOpenKey === item.key}
          onOpenChange={(open) => setCollapsedOpenKey(open ? item.key : null)}
        >
          <div className={`yx-nav-collapsed-icon${active ? ' active' : ''}`}>{glyph}</div>
        </Popover>
      );
    }
    const active = isPathActive(item.path, activePath);
    return (
      <Tooltip key={item.key} title={item.label} placement="right">
        <div
          className={`yx-nav-collapsed-icon${active ? ' active' : ''}`}
          onClick={() => {
            if (item.path) onNavigate(item.path);
          }}
        >
          {glyph}
        </div>
      </Tooltip>
    );
  };

  /** 折叠态顶层：每个顶层项（或其子项）渲染为分隔线 + 图标入口，保留分组视觉与全部入口可达 */
  const renderCollapsedItems = (): React.ReactNode =>
    navItems.map((item) => (
      <Fragment key={item.key}>
        <div className="yx-nav-collapsed-sep" />
        {item.children?.length ? item.children.map(renderCollapsedEntry) : renderCollapsedEntry(item)}
      </Fragment>
    ));

  return (
    <Layout className="yx-app-layout" data-collapsed={collapsed}>
      <Layout.Header className="yx-topbar">
        <div className="yx-topbar-left">
          <a
            className="yx-topbar-brand"
            href={logo.href}
            onClick={(e) => {
              e.preventDefault();
              onNavigate(logo.href);
            }}
            aria-label={logo.alt}
          >
            {logo.text ? (
              <span className="yx-topbar-brand-text">{logo.text}</span>
            ) : (
              <img src={logo.locale === 'en' ? '/logo-en.svg' : '/logo.svg'} alt={logo.alt} />
            )}
          </a>
        </div>
        <div className="yx-topbar-right">
          {topbarRight?.custom != null && <>{resolveSlot(topbarRight.custom, ctx)}</>}
          {lang && (
            <Dropdown
              menu={{
                items: LANGUAGE_VALUES.filter((value) => value !== lang.current).map((value) => ({
                  key: value,
                  label: localeName(lang.current, value),
                })),
                onClick: ({ key }) => lang.onSwitch(key),
              }}
              placement="bottomRight"
              trigger={['click']}
            >
              <Button
                type="text"
                style={{
                  height: collapsed ? 32 : 38,
                  borderRadius: 10,
                  display: 'flex',
                  alignItems: 'center',
                  gap: collapsed ? 0 : 4,
                  fontSize: 14,
                  color: 'inherit',
                  padding: collapsed ? '0 8px' : '0 10px',
                }}
              >
                <GlobalOutlined style={{ fontSize: 16 }} />
                {!collapsed && (
                  <>
                    <span>
                      {lang.current != null ? localeName(lang.current, lang.current) : null}
                    </span>
                    <CaretDownOutlined style={{ fontSize: 10, color: 'var(--yx-text-muted)' }} />
                  </>
                )}
              </Button>
            </Dropdown>
          )}
          {user && (() => {
            const wrap = (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: collapsed ? 0 : 10,
                  cursor: 'pointer',
                  padding: collapsed ? '4px' : '4px 12px 4px 4px',
                  borderRadius: 10,
                }}
              >
                <div className="yx-user-avatar">{user.avatarText.charAt(0).toUpperCase()}</div>
                {!collapsed && user.name != null && (
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <span style={{ fontSize: 14, fontWeight: 500, color: 'var(--yx-text-primary)' }}>
                      {user.name}
                    </span>
                    {user.subtitle != null && (
                      <span style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>{user.subtitle}</span>
                    )}
                  </div>
                )}
              </div>
            );
            return user.actions && user.actions.length > 0 ? (
              <Dropdown
                menu={{
                  items: user.actions.map((a) => ({ key: a.key, icon: a.icon, label: a.label, onClick: a.onClick })),
                }}
                placement="bottomRight"
              >
                {wrap}
              </Dropdown>
            ) : (
              wrap
            );
          })()}
        </div>
      </Layout.Header>
      <Layout className="yx-app-body">
        <Layout.Sider
          className="yx-sidebar"
          theme="light"
          width={SIDEBAR_WIDTH}
          collapsedWidth={SIDEBAR_COLLAPSED_WIDTH}
          collapsed={collapsed}
          breakpoint="md"
          onBreakpoint={(broken) => setCollapsed(broken)}
        >
          <nav className="yx-sidebar-nav">
            {sidebarExtra != null && <div className="yx-sidebar-slot">{resolveSlot(sidebarExtra, ctx)}</div>}
            {navLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: '32px 0' }}>
                <Spin size="small" />
              </div>
            ) : collapsed ? (
              renderCollapsedItems()
            ) : (
              navItems.map(renderTopItem)
            )}
          </nav>
          {/* 折叠钮：骑在侧栏右边界、垂直居中，故自渲染而不复用 antd 自带 trigger ——
              后者宽度是 Sider 以内联 style 写死的「侧栏宽度」、且固定 position:fixed bottom:0，
              表达不了「窄条压在边界线上」。折叠状态仍由本组件持有（受控 collapsed + 此处 setCollapsed），
              因此不再需要 antd 的 collapsible / trigger / onCollapse 三个 prop。
              用原生 button 而非 antd Button：形状/尺寸/配色整套自定义（13×42 半圆端胶囊），
              antd Button 的 token 推导在这个尺寸下反而会带来要逐条覆盖的默认值（实测推成 27×28 非正圆），
              hover 反馈也交给 CSS（.yx-sider-toggle:hover），不再需要 React state 驱动图标切换。 */}
          {showCollapseTrigger && (
            <button
              type="button"
              className="yx-sider-toggle"
              onClick={() => setCollapsed((prev) => !prev)}
            >
              {/* 箭头指向侧栏接下来要移动的方向：展开态点它收起（左箭头），折叠态点它展开（右箭头） */}
              {collapsed ? <RightOutlined /> : <LeftOutlined />}
            </button>
          )}
        </Layout.Sider>
        <Layout.Content className="yx-content">{children}</Layout.Content>
      </Layout>
    </Layout>
  );
}
