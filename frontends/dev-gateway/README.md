# ============================================================
# Jonex Dev Gateway
#
# 一个 Node.js 开发网关，替代生产环境的 Nginx frontend-gateway。
# 在本地开发时提供统一的 HTTP 入口，将请求路由到各个
# Vite 开发服务器和后台 API 网关。
# ============================================================

## 动机

生产环境通过 `deploy/docker/frontend-gateway.Dockerfile` + `deploy/nginx/app-locations.conf`
使用 Nginx 作为唯一对外前端入口。开发时需要同样的路由能力，
但不想依赖 Docker / Nginx，因此用 Node.js http-proxy 实现轻量替代。

## 架构

```
                        ┌──────────────────┐
                        │  Dev Gateway      │
                        │  :8080            │
                        └──────┬───────────┘
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
    ┌─────▼──────┐   ┌─────────▼──────────┐   ┌──────▼──────┐
    │  /api/*     │   │  /* (catch-all)    │   │  /<app>/*   │
    │             │   │  /remotes/<app>/*  │   │  standalone │
    └─────┬──────┘   └─────────┬──────────┘   └──────┬──────┘
          │                    │                      │
    ┌─────▼──────┐   ┌─────────▼──────────┐   ┌──────▼──────────┐
    │  API GW    │   │  Shell Vite Dev    │   │  Sub-app Vite   │
    │  :8000     │   │  :5173             │   │  :5174-5177     │
    └────────────┘   └───────────────────┘   └─────────────────┘
```

## 启动方式

```bash
# 1. Install 依赖
pnpm install

# 2. 可选：拷贝环境变量配置
cp .env.example .env
# 编辑 .env 中的目标地址

# 3. 启动所有前端 dev server（新开终端）
pnpm --filter @jonex/shell dev
pnpm --filter @jonex/expert-call dev
pnpm --filter @jonex/core-business dev
pnpm --filter @jonex/platform-management dev
pnpm --filter @jonex/ecosystem-management dev

# 4. 启动开发网关
pnpm dev     # 自动重启
# 或
pnpm start   # 单次运行

# 5. 浏览器打开 http://localhost:8080
```

## 登录相关配置

启用统一入口后，前端登录配置也要切到统一入口模式，否则会出现：

- 从 `http://localhost:8080/<app>/...` 进入时，被重定向到 `http://localhost:5173/login`
- 登录成功后，Shell 拒绝回跳到 `8080`，页面提示失败

本地开发建议同步满足以下条件：

- 各子应用 `VITE_LOGIN` 指向 `http://localhost:8080/login`
- Shell 的 `VITE_ALLOWED_REDIRECT_ORIGINS` 包含 `http://localhost:8080`
- 后端 `AUTH_ALLOWED_REDIRECT_URIS` 为各 appId 补充 `http://localhost:8080/<app>/` 前缀

## 路由表

| Path | Upstream |
|------|----------|
| `/health` | 返回 `200 OK` |
| `/api/*` | `API_TARGET` (默认 :8000) |
| `/remotes/expert-call/*` | 去掉 `/remotes` → Expert Call dev server (:5174) |
| `/remotes/core-business/*` | 去掉 `/remotes` → Core Business dev server (:5175) |
| `/remotes/ecosystem-management/*` | 去掉 `/remotes` → Eco Management dev server (:5176) |
| `/remotes/platform-management/*` | 去掉 `/remotes` → Platform Mgmt dev server (:5177) |
| `/expert-call/*` | Expert Call dev server (:5174) |
| `/core-business/*` | Core Business dev server (:5175) |
| `/ecosystem-management/*` | Eco Management dev server (:5176) |
| `/platform-management/*` | Platform Mgmt dev server (:5177) |
| `/*` (catch-all) | Shell dev server (:5173) |

## 环境变量

参见 [`.env.example`](./.env.example)。

## 与 Nginx 配置的对应关系

Nginx `app-locations.conf` 中的路由规则被原样映射到
本网关，唯一区别是：

- 开发时连接 localhost Vite dev server 而非 Docker 容器
- `/remotes/<app>/` 路径去掉 `/remotes` 前缀后转发给子应用，
  因为 Vite dev server 的 `base` 是 `/<app>/`（例如 `expert-call/`）
- 添加了 WebSocket upgrade 支持，保障 Vite HMR 正常运作
- 保留了部分安全响应头（X-Frame-Options 等）
