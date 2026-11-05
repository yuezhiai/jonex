import { defineConfig } from 'vitest/config';
import path from 'path';
import { fileURLToPath } from 'url';

// ⚠️ 必须用 fileURLToPath，**不能**用 `new URL(import.meta.url).pathname`：
// Windows 上 pathname 是 `/C:/tyyz-python/...`，再经 path.resolve 会拼成
// `C:\C:\tyyz-python\...` —— root 指向一个不存在的目录，vitest 会以
// 「No test files found」退出（而不是报配置错），很容易被当成「没有测试」。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pathSrc = path.resolve(__dirname, 'src');

export default defineConfig({
  root: __dirname,
  resolve: {
    alias: {
      '@': pathSrc,
      '@jonex/shell-sdk': path.resolve(__dirname, '../shared/shell-sdk/src/index.ts'),
      '@jonex/platform-theme': path.resolve(__dirname, '../shared/platform-theme/src'),
    },
  },
  test: {
    globals: true,
    environment: 'node',
    // 组件测试（*.test.tsx）跑 jsdom（RTL 需要真实 DOM 与 window）；纯逻辑
    // 测试（*.test.ts）保持 node —— 既有 50 条不复用任何 DOM 假设。
    environmentMatchGlobs: [['src/**/*.test.tsx', 'jsdom']],
    setupFiles: ['./src/test/setup.ts'],
    // jsdom 下 antd 组件的挂载/交互（cssinjs 注入、门户、无障碍树查询）比 node
    // 纯逻辑测试慢一个量级，默认 5s 会误伤 —— 单文件放宽到 20s。
    testTimeout: 20000,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    server: {
      deps: {
        inline: ['@jonex/shell-sdk', '@jonex/platform-theme'],
      },
    },
  },
});
