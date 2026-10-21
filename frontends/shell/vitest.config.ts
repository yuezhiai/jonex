import { defineConfig } from 'vitest/config';
import path from 'path';
import { fileURLToPath } from 'url';

// Windows 上 `new URL(import.meta.url).pathname` 是 `/C:/...`，再经 path.resolve
// 会拼成 `C:\C:\...`，root 指向不存在的目录，vitest 会以「No test files found」退出。
// 与 core-business / platform-management 的修复保持同一写法。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pathSrc = path.resolve(__dirname, 'src');

/**
 * 与 vite.config.js 的 alias 保持一致：三处按源码路径解析，测试与构建才不会
 * 出现「构建通过、测试失败」的双份实现。刻意不含 jsdom —— shell 的单测都是
 * 纯工具函数，environment 保持 node 可省一个重依赖，需要 window 的用例用
 * vi.stubGlobal 自行注入。
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
    // @jonex/shell-sdk 也被收进来：它没有自己的 vitest 依赖与 test 脚本（构建只跑 tsc），
    // 单测若只留在它自己目录里就**永远不会被执行** —— 那等于把 S1 / S9 的断言挂在墙上的
    // 装饰品。本仓库里 shell 是它最重的消费方，且下面 alias 已把它指到源码（同一份实现
    // 就是被测对象），故由 shell 的这套配置一起跑。子应用（core-business 等）各自有配置，
    // 不重复收。
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', '../shared/shell-sdk/src/**/*.test.ts'],
    server: {
      deps: {
        inline: ['@jonex/shell-sdk', '@jonex/platform-theme', '@jonex/shared-lib'],
      },
    },
  },
});
