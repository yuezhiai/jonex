import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import federation from '@originjs/vite-plugin-federation';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Vite 4 的依赖预构建缓存（`deps/_metadata.json`）记录了每个依赖的 src 路径，但缓存目录和
 * metadata.hash 都不会因为 lockfile 变化而整体失效；pnpm 装包重建 `.pnpm` 虚拟目录后，旧 src
 * 指向的文件夹会消失，之后每次重优化都在 prepareEsbuildOptimizerRun 里抛 ENOENT → 预构建永远
 * 提交不上 → 浏览器持续收到 504 Outdated Optimize Dep（再叠加 loadable 的自动刷新就是无限重载）。
 * 启动时检查一遍：只要有 src 已不存在，就把该 app 的缓存整目录删掉重建，避免踩进这个死循环。
 */
function resetBrokenDepsCache(cacheDir: string) {
  const metadataPath = path.resolve(cacheDir, 'deps/_metadata.json');
  try {
    if (!fs.existsSync(metadataPath)) {
      return;
    }
    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf-8')) as {
      optimized?: Record<string, { src?: string }>;
    };
    const depsDir = path.dirname(metadataPath);
    const missing = Object.values(metadata.optimized ?? {}).filter((info) => {
      const src = info?.src;
      // 只校验相对路径形态的 src（pnpm/npm 解析结果），裸模块名交给 Vite 自己解析
      return typeof src === 'string' && src.startsWith('.') && !fs.existsSync(path.resolve(depsDir, src));
    });
    if (missing.length > 0) {
      fs.rmSync(cacheDir, { recursive: true, force: true });
      console.warn(
        `[vite-deps-cache] ${cacheDir} 引用了 ${missing.length} 个已不存在的依赖源（pnpm 重建过 node_modules），` +
          '已删除该缓存，本次启动会重新做依赖预构建。',
      );
    }
  } catch (e) {
    // 缓存目录读不了就当没有缓存，交给 Vite 自己重建
    console.warn('[vite-deps-cache] 校验预构建缓存失败，忽略：', e instanceof Error ? e.message : e);
  }
}

export interface DefineAppConfigOptions {
  /** 子应用 id，用于 hosted base 路径与 src 别名解析，如 'core-business' */
  appId: string;
  /** Module Federation scope（camelCase），如 'coreBusiness' */
  scope: string;
  /** dev / preview 端口 */
  port: number;
}

/**
 * 业务子应用统一 Vite 配置工厂。
 *
 * 三份子应用配置（core-business / platform-management / ecosystem-management）
 * 曾经完全同构，仅 base / federation.name / port 三个值不同。统一在此收敛，
 * 子应用侧只需声明差异参数。详见 docs/optimization/FE-OPTIMIZATION.md 第四步。
 */
export function defineAppConfig({ appId, scope, port }: DefineAppConfigOptions) {
  const pathSrc = path.resolve(__dirname, appId, 'src');

  const parentEnv = loadEnv('', __dirname, 'VITE_API_');
  const apiTarget = process.env.VITE_API_TARGET || parentEnv.VITE_API_TARGET || 'http://localhost:8000';

  return defineConfig(({ command }) => {
    // 依赖预构建 / 构建缓存按 appId 隔离：多 dev server 共享同一 node_modules/.vite 会并发覆盖磁盘产物，
    // 各 server 内存批次与磁盘脱节、同一物理 chunk 被以不同 URL serve → 双 React 实例 → 子应用白屏。
    const cacheDir = path.resolve(__dirname, `node_modules/.vite/${appId}`);
    if (command === 'serve') {
      resetBrokenDepsCache(cacheDir);
    }

    return {
      base: `/${appId}/`,
      cacheDir,
      plugins: [
        react(),
        federation({
          name: scope,
          filename: 'remoteEntry.js',
          exposes: {
            './Mount': './src/remote/RemoteApp.tsx',
          },
          shared: {
            react: { singleton: true, requiredVersion: '^18.2.0' },
            'react-dom': { singleton: true, requiredVersion: '^18.2.0' },
            // antd 系列必须 singleton 共享（详见 shell/vite.config.js 说明）：
            // 避免 shell 与 remote 各带一份 antd/cssinjs、跨实例误删 head 样式，
            // 导致 popup z-index 变量丢失、弹层被盖住。
            antd: { singleton: true, requiredVersion: '^6.4.3' },
            '@ant-design/icons': { singleton: true, requiredVersion: '^6.2.3' },
          },
        }),
      ],
      server: {
        host: '0.0.0.0',
        port,
        proxy: {
          '/api/': {
            target: apiTarget,
            changeOrigin: true,
          },
        },
      },
      preview: {
        host: '0.0.0.0',
        port,
      },
      // 依赖预构建（dev）：默认只从 index.html 的静态 import 图扫描，loadable 懒加载页面
      // （如 pages/**）里新引入的三方依赖不会进首批预构建，要等浏览器首次请求该页面才被"发现"。
      // 那次运行时重优化一旦没提交成功（Vite 4 的 cancel/rerun 竞态），浏览器会持续拿到
      // 504 Outdated Optimize Dep，配合 loadable 的自动刷新就是无限重载（知识库设置页踩过）。
      // 把扫描入口放宽到整个 src（排除测试/story 文件，避免把 vitest 这类 node-only 包卷进预构建），
      // 让新增依赖在 dev 启动时就进入首批预构建，从源头断掉运行时发现路径。
      optimizeDeps: {
        entries: ['index.html', 'src/**/!(*.test|*.spec|*.stories).{ts,tsx}'],
        // 这两个是懒加载页面里的重型依赖，显式 include 双保险；同时 Vite 4 的依赖缓存 hash
        // 只算 include/exclude/esbuildOptions（**不算 entries**），改 entries 不会让已有缓存失效，
        // 这里动 include 才能让老环境的 node_modules/.vite/<app> 在下次启动时自动重建。
        include: ['@xyflow/react', 'immer'],
      },
      resolve: {
        alias: {
          '@': `${pathSrc}`,
          '@jonex/shell-sdk': path.resolve(__dirname, 'shared/shell-sdk/src/index.ts'),
          '@jonex/platform-theme': path.resolve(__dirname, 'shared/platform-theme/src'),
          '@jonex/shared-lib': path.resolve(__dirname, 'shared/shared-lib/src/index.ts'),
        },
      },
      build: {
        target: 'esnext',
        rollupOptions: {
          output: {
            chunkFileNames: 'assets/[name]-[hash].js',
            entryFileNames: 'assets/[name]-[hash].js',
            assetFileNames: 'assets/[name]-[hash].[ext]',
          },
        },
      },
    };
  });
}
