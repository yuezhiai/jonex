import { defineConfig } from 'vitest/config';
import path from 'path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pathSrc = path.resolve(__dirname, 'src');

/**
 * 与 vite.config.ts（走 vite.base.config.ts 的 defineAppConfig）的 alias 保持一致：
 * 三处按**源码路径**解析，测试与构建才不会出现「构建通过、测试失败」的双份实现。
 * environment 保持 node —— 本包单测是纯 api 模块（axios adapter 桩），
 * 需要 window / localStorage 的用例用 vi.stubGlobal 自行注入（同 shell 的写法）。
 */
export default defineConfig({
  root: __dirname,
  resolve: {
    alias: {
      '@': pathSrc,
      '@jonex/shell-sdk': path.resolve(__dirname, '../shared/shell-sdk/src/index.ts'),
      '@jonex/platform-theme': path.resolve(__dirname, '../shared/platform-theme/src'),
      '@jonex/shared-lib': path.resolve(__dirname, '../shared/shared-lib/src/index.ts'),
    },
  },
  test: {
    globals: true,
    environment: 'node',
    // jsdom 下 antd 组件的挂载/交互（cssinjs 注入、门户、无障碍树查询）比 node
    // 纯逻辑测试慢一个量级，默认 5s 会误伤（演化台 RunDetail / StartRunModal 已踩到）
    // —— 与 core-business 同口径，单测放宽到 20s。
    testTimeout: 20000,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    server: {
      deps: {
        inline: ['@jonex/shell-sdk', '@jonex/platform-theme', '@jonex/shared-lib'],
      },
    },
  },
});
