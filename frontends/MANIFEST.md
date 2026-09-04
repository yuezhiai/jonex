# 前端应用清单

本文档记录当前前端应用的运行约定。生产环境的应用清单以平台后端注册表为准，由 `GET /api/v1/platform/frontend/apps` 输出；`frontends/shell/public/app-manifest.json` 只作为本地开发和后端不可用时的 fallback。

新前端子应用必须遵循根目录 [frontend-development-standard.md](../frontend-development-standard.md)。

## Shell

| 项 | 值 |
|---|---|
| 包名 | `@jonex/shell` |
| 职责 | 登录、全局导航、应用清单加载、权限守卫、子应用挂载 |
| 访问路径 | `/` |
| 开发端口 | `5173` |
| 生产容器 | `shell-frontend` |

## 子应用

| 应用 | 包名 | hosted 路径 | standalone 路径 | remote entry | scope | 端口 |
|---|---|---|---|---|---|---|
| 核心业务 | `@jonex/core-business` | `/apps/core-business` | `/core-business/` | `/remotes/core-business/assets/remoteEntry.js` | `coreBusiness` | `5175` |
| 平台管理 | `@jonex/platform-management` | `/apps/platform-management` | `/platform-management/` | `/remotes/platform-management/assets/remoteEntry.js` | `platformManagement` | `5177` |
| 生态管理 | `@jonex/ecosystem-management` | `/apps/ecosystem-management` | `/ecosystem-management/` | `/remotes/ecosystem-management/assets/remoteEntry.js` | `ecosystemManagement` | `5176` |
| 专家访谈 | `@jonex/expert-call` | `/apps/expert-call` | `/expert-call/` | `/remotes/expert-call/assets/remoteEntry.js` | `expertCall` | `5174` |

## API 约定

- 前端只能调用 `/api/v1/**`。
- 专家访谈统一调用 `/api/v1/expert-call/**`。
- 不允许新增业务 API 兼容前缀。
- 不允许前端直连 Sidecar、能力服务容器名或宿主调试端口。

## 变更流程

1. 在平台后端注册应用、菜单、权限和 remote 元数据。
2. 同步更新 `frontends/shell/public/app-manifest.json` 作为本地 fallback。
3. 在 `deploy/nginx/app-locations.conf` 配置 standalone 路径和 remote assets 反代。
4. 新增或调整子应用自身 `Dockerfile`、`nginx/default.conf`、`vite.config.ts`（业务子应用继承 `frontends/vite.base.config.ts` 的 `defineAppConfig`，`tsconfig.json` extends `frontends/tsconfig.base.json`）。
5. 在 `frontends/` 根目录运行 `pnpm run typecheck` 和 `pnpm run build`，必要时再用 `pnpm --filter <package> typecheck` 定位单个子应用问题。
