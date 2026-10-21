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
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    server: {
      deps: {
        inline: ['@jonex/shell-sdk', '@jonex/platform-theme'],
      },
    },
  },
});
