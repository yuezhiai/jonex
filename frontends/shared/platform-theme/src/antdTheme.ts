import { colors, radius, typography } from './tokens';
import { createCache } from '@ant-design/cssinjs';

/**
 * 全局共享的 cssinjs cache（跨所有应用统一同一实例）。
 *
 * 通过 window 全局持有，即使各应用各自打包一份 @ant-design/cssinjs 模块，
 * cache 对象仍是同一个。配合 `<StyleProvider cache={sharedCache}>` 后：
 * 1. 所有 `<style>` 打上同一 instanceId（删除时校验一致，不跨实例误删）
 * 2. useGlobalCache 引用计数全局一致（主应用下拉短暂卸载时，页面内其它 popup
 *    组件仍持有引用，组件 token 变量标签 .antd.ant-dropdown-css-var 不会被误删）
 */
const globalObj = (typeof window !== 'undefined' ? window : globalThis) as {
  __JONEX_CSSINJS_CACHE__?: ReturnType<typeof createCache>;
};
globalObj.__JONEX_CSSINJS_CACHE__ = globalObj.__JONEX_CSSINJS_CACHE__ || createCache();
export const sharedCache: ReturnType<typeof createCache> = globalObj.__JONEX_CSSINJS_CACHE__;

export const antdTheme = {
  token: {
    colorPrimary: colors.accent,
    colorPrimaryHover: colors.accentHover,
    colorPrimaryActive: colors.brandBlue,
    colorPrimaryBg: colors.infoBg,
    colorBgLayout: colors.bg,
    colorBgContainer: colors.white,
    colorBgElevated: colors.white,
    colorBorder: colors.border,
    colorBorderSecondary: colors.borderTable,
    colorText: colors.textPrimary,
    colorTextSecondary: colors.textSecondary,
    colorTextTertiary: colors.textMuted,
    fontFamily: typography.fontFamily,
    borderRadius: radius.btn,
    borderRadiusLG: radius.card,
    controlHeight: 36,
    fontSize: typography.bodySize,
    fontSizeLG: typography.bodySize,
    lineHeight: 1.6,
    paddingContentHorizontal: 24,
  },
  components: {
    Layout: {
      bodyBg: colors.bg,
      headerBg: colors.white,
      headerHeight: 64,
      siderBg: colors.brandDark,
    },
    Menu: {
      darkItemBg: 'transparent',
      darkItemColor: colors.sidebarText,
      darkItemHoverBg: colors.sidebarHover,
      darkItemSelectedBg: colors.sidebarActive,
      darkItemSelectedColor: colors.white,
      itemBorderRadius: 0,
      itemHeight: 42,
      iconSize: 15,
    },
    Card: {
      paddingLG: 24,
      borderRadiusLG: radius.card,
    },
    Button: {
      borderRadius: radius.btn,
      primaryColor: colors.white,
      primaryShadow: 'none',
      defaultBorderColor: colors.border,
      defaultColor: colors.textSecondary,
      defaultBg: colors.white,
      fontWeight: 500,
    },
    Table: {
      headerColor: colors.textSecondary,
      headerBorderRadius: 0,
      rowHoverBg: colors.rowHover,
      borderColor: colors.rowBorder,
      cellPaddingBlock: 12,
      cellPaddingInline: 16,
      fontSize: typography.bodySize,
    },
    Input: {
      borderRadius: radius.input,
      activeBorderColor: colors.accent,
      activeShadow: '0 0 0 3px rgba(59,130,246,0.1)',
    },
    Select: {
      borderRadius: radius.input,
    },
    Tabs: {
      inkBarColor: colors.accent,
      itemActiveColor: colors.accent,
      itemHoverColor: colors.accent,
      itemColor: colors.textSecondary,
      horizontalItemGutter: 0,
    },
    Tag: {
      borderRadiusSM: radius.tag,
    },
    Breadcrumb: {
      itemColor: colors.textSecondary,
      lastItemColor: colors.brandBlue,
      linkColor: colors.textSecondary,
      linkHoverColor: colors.accent,
    },
    Spin: {
      colorPrimary: colors.accent,
    },
  },
};
