import type { ThemeConfig } from 'antd';

/**
 * 全局 antd 主题定制入口（ConfigProvider 的 theme）。
 *
 * 当前刻意留空：平台已回归 antd 默认外观，原先这里的 token / components 定制
 * 和 layout.css 里的 .ant-* 全局覆盖都已移除。
 *
 * 后续需要统一调整组件样式时，在这里集中配置 token / components —— 四个子应用
 * 与 _template 的 ConfigProvider 都已接线到本对象，改这一处即全量生效。
 * 不要在页面样式里新增 .ant-* 全局覆盖。
 */
export const antdTheme: ThemeConfig = {};

/**
 * 给 ConfigProvider 的 theme 指定稳定的 cssVar key。
 *
 * 为什么必须显式指定：antd v6 恒开 cssVar，变量以 `css-var-<key>` 为作用域写进
 * `<style data-token="<key>">`。不指定时 key 由 `React.useId()` 生成 —— 每个 React
 * 根各自从 `:r0` 开始计数，Module Federation 下 shell 与子应用是两个根，于是两边
 * 都拿到 `css-var-r0`：同一个 key 对应两份 token 缓存，子应用根卸载时 cssinjs
 * 的 `tokenKeys` 引用计数归零，把 shell 仍在用的 `<style data-token="css-var-r0">`
 * 一并删掉（现象：切路由后再打开 shell 侧栏的切换空间下拉框，`--ant-*` 全部解析成
 * 空，item 高度 18px、背景透明、与侧栏文字重叠）。子应用重新挂载时 useId 又变成
 * `:r1i`，key 一变变量标签也换作用域，问题不会自愈。
 *
 * 因此每个 ConfigProvider 都固定一个 app 级 key（同一应用 standalone / remote
 * 两个入口可复用，二者不会同时挂载；但不同应用必须不同，因为它们会与 shell 同时存在）。
 */
export function withCssVarKey(key: string): ThemeConfig {
  return {
    ...antdTheme,
    cssVar: { ...antdTheme.cssVar, key },
  };
}
