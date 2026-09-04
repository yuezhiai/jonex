# Jonex Frontend App Template

本模板用于创建新的Jonex平台前端子应用。新应用必须遵循根目录 [frontend-development-standard.md](../../frontend-development-standard.md)。

## 1. 创建应用

复制模板后替换：

| 占位符 | 示例 |
|---|---|
| `{{APP_NAME}}` | `@jonex/my-app` |
| `{{APP_ID}}` | `my-app` |
| `{{APP_TITLE}}` | `我的应用` |
| `{{REMOTE_SCOPE}}` | `myApp` |
| `{{DEV_PORT}}` | `5180` |

标准路径：

```text
hosted:      /apps/{app-id}
standalone:  /{app-id}/
remote:      /remotes/{app-id}/assets/remoteEntry.js
api:         /api/v1/{capability}/**
```

## 2. 目录结构

```text
src/
├── app/                 # 应用级 Provider、错误边界、启动配置
├── router/              # 路由和页面元信息
├── remote/              # Module Federation mount 入口
├── pages/               # 页面编排
├── features/            # 复杂业务流程
├── components/          # 应用内复用组件
├── services/            # API client 和业务请求函数
├── stores/              # 跨页面客户端状态
├── hooks/               # 组合逻辑
├── types/               # 当前应用类型
├── utils/               # 纯函数工具
└── styles/              # 应用级样式
```

## 3. Shell 接入

子应用必须支持两种模式：

| 模式 | 地址 | Router | 登录态 |
|---|---|---|---|
| hosted | `/apps/{app-id}` | MemoryRouter | ShellContext 注入 |
| standalone | `/{app-id}/` | BrowserRouter | `@jonex/shell-sdk` 读取 |

`src/remote/RemoteApp.tsx` 应导出 mount 能力，并返回幂等 cleanup 函数。Hosted 模式缺少登录态时不要自行跳转登录页，由 Shell 处理。

## 4. 共享依赖

模板默认使用：

- `@jonex/platform-theme`：平台 CSS tokens、Ant Design theme、布局样式。
- `@jonex/shell-sdk`：认证存储、跳转工具、ShellContext、共享 manifest 类型。

子应用不要自定义一套品牌色、全局 reset、认证存储或应用清单类型。

## 5. API 规则

- 页面和组件不得直接写 `fetch` 或 `axios`。
- API 调用必须进入 `services/`。
- 前端只调用 `/api/v1/**`。
- 业务请求 body 不传 `tenant_id`。
- 不直连 Sidecar、capability service、容器名或宿主调试端口。

## 6. 接入清单

新增应用时同步：

1. `frontends/pnpm-workspace.yaml`。
2. 平台后端应用注册表。
3. `frontends/shell/public/app-manifest.json` 本地 fallback。
4. `deploy/nginx/app-locations.conf`。
5. 子应用 `Dockerfile` 和 `nginx/default.conf`。
6. `frontends/MANIFEST.md`。

## 7. 检查

```bash
pnpm --filter @jonex/{app-id} typecheck
pnpm --filter @jonex/{app-id} build
```

如果应用有 lint 脚本，也必须运行：

```bash
pnpm --filter @jonex/{app-id} lint
```
