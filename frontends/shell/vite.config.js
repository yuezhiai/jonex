import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import federation from '@originjs/vite-plugin-federation'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const parentEnv = loadEnv('', path.resolve(__dirname, '..'), 'VITE_API_')
const apiTarget = process.env.VITE_API_TARGET || parentEnv.VITE_API_TARGET || 'http://localhost:8000'

export default defineConfig({
  base: '/',
  // 依赖预构建缓存按应用隔离：多 dev server 共享 node_modules/.vite 会并发覆盖磁盘产物，
  // 导致同一物理 chunk 被以不同 URL serve → 双 React 实例 → 子应用白屏
  cacheDir: path.resolve(__dirname, '../node_modules/.vite/shell'),
  resolve: {
    alias: {
      '@jonex/shell-sdk': path.resolve(__dirname, '../shared/shell-sdk/src/index.ts'),
      '@jonex/platform-theme': path.resolve(__dirname, '../shared/platform-theme/src'),
      // 指向 src 而非 dist：Docker 构建上下文里 shared-lib 的 dist 被 .dockerignore 排除，
      // 与 vite.base.config 统一走源码编译
      '@jonex/shared-lib': path.resolve(__dirname, '../shared/shared-lib/src/index.ts'),
    },
  },
  plugins: [
    react(),
    federation({
      name: 'shell',
      remotes: {},
      shared: {
        react: { singleton: true, requiredVersion: '^18.2.0' },
        'react-dom': { singleton: true, requiredVersion: '^18.2.0' },
        // antd 系列必须 singleton 共享：否则 shell 与各 remote 各自打包一份
        // antd + cssinjs，两套 cssinjs 往同一个 document.head 写样式并按 data-*-hash
        // 去重，先挂载方回收 <style> 时会误删另一方仍依赖的样式（表现为 popup
        // z-index 变量丢失、弹层被内容盖住）。共享后全窗口只有一份 antd/cssinjs。
        antd: { singleton: true, requiredVersion: '^6.4.3' },
        '@ant-design/icons': { singleton: true, requiredVersion: '^6.2.3' },
      },
    }),
  ],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // —— 后端 API ——
      '/api/': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/health': {
        target: apiTarget,
        changeOrigin: true,
      },
      // —— 子应用 remote assets（Module Federation） ——
      // 请求 /remotes/xxx/... → 子应用 vite dev server，去掉 /remotes 前缀
      '/remotes/expert-call/': {
        target: 'http://localhost:5174',
        changeOrigin: true,
        rewrite: (path) =>
          path.includes('remoteEntry.js')
            ? '/@id/__x00__virtual:__federation_remote_expertCall_entry' + (path.includes('?') ? path.substring(path.indexOf('?')) : '')
            : path.replace(/^\/remotes/, ''),
      },
      '/remotes/core-business/': {
        target: 'http://localhost:5175',
        changeOrigin: true,
        rewrite: (path) =>
          path.includes('remoteEntry.js')
            ? '/@id/__x00__virtual:__federation_remote_coreBusiness_entry' + (path.includes('?') ? path.substring(path.indexOf('?')) : '')
            : path.replace(/^\/remotes/, ''),
      },
      '/remotes/platform-management/': {
        target: 'http://localhost:5177',
        changeOrigin: true,
        rewrite: (path) =>
          path.includes('remoteEntry.js')
            ? '/@id/__x00__virtual:__federation_remote_platformManagement_entry' + (path.includes('?') ? path.substring(path.indexOf('?')) : '')
            : path.replace(/^\/remotes/, ''),
      },
      '/remotes/ecosystem-management/': {
        target: 'http://localhost:5176',
        changeOrigin: true,
        rewrite: (path) =>
          path.includes('remoteEntry.js')
            ? '/@id/__x00__virtual:__federation_remote_ecosystemManagement_entry' + (path.includes('?') ? path.substring(path.indexOf('?')) : '')
            : path.replace(/^\/remotes/, ''),
      },
      // —— 子应用 standalone SPA 回退 ——
      '/expert-call/': {
        target: 'http://localhost:5174',
        changeOrigin: true,
      },
      '/core-business/': {
        target: 'http://localhost:5175',
        changeOrigin: true,
      },
      '/platform-management/': {
        target: 'http://localhost:5177',
        changeOrigin: true,
      },
      '/ecosystem-management/': {
        target: 'http://localhost:5176',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'esnext',
  },
})
