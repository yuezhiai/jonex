import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import federation from '@originjs/vite-plugin-federation';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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

  return defineConfig(() => {
    return {
      base: `/${appId}/`,
      // 依赖预构建 / 构建缓存按 appId 隔离：多 dev server 共享同一 node_modules/.vite 会并发覆盖磁盘产物，
      // 各 server 内存批次与磁盘脱节、同一物理 chunk 被以不同 URL serve → 双 React 实例 → 子应用白屏。
      cacheDir: path.resolve(__dirname, `node_modules/.vite/${appId}`),
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
