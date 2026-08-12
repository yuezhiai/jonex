# 前端共享库抽取方案（P0：`@jonex/shared-lib`）

> 目标：消除 4 个子应用间的重复代码，统一工具函数、hooks、基础组件。
> 对应优化分析文档：`frontends/FE-OPTIMIZATION.md` 第一节。

## 一、目标与收益

- **删除 4 个应用内 20+ 重复文件**，只维护一份
- **统一 API 请求封装**（当前 3 套不同写法）
- **统一 hooks / 工具函数**，避免各应用行为漂移
- 后续新增应用（_template 复制）直接复用，不再拷贝

## 二、共享包结构设计

```text
frontends/shared/shared-lib/
  ├── package.json                # @jonex/shared-lib, workspace:*
  ├── src/
  │   ├── index.ts                # 统一出口
  │   ├── utils/
  │   │   ├── storage.ts          # localStorage / cookie 封装
  │   │   ├── safeMessage.ts      # 安全 message（XSS 过滤 + 节流）
  │   │   ├── misc.ts             # getAvatarText / clearLocalStorageExcept 等
  │   │   └── loadable.tsx        # 路由懒加载 + chunk 失败重试
  │   ├── hooks/
  │   │   ├── useIsMobile.tsx     # 移动端检测
  │   │   ├── useDocumentTitle.ts # 路由标题同步（需参数化）
  │   │   └── usePageMeta.ts      # 页面元信息（需参数化）
  │   ├── components/
  │   │   ├── ConfirmDialog.tsx   # 通用确认弹窗
  │   │   └── LoadingFallback.tsx # 页面加载占位（Shell 同款）
  │   └── api/
  │       └── request.ts          # axios 统一封装（token/locale/错误处理）
  └── tsconfig.json
```

## 三、文件清单与依赖分析

### A 类：可直接抽取（无应用依赖）— 优先做

| 文件 | 外部依赖 | 迁移方式 |
|---|---|---|
| `utils/storage.ts` | `@/utils/utils`（clearLocalStorageExcept） | 连带抽 `clearLocalStorageExcept` 到 `misc.ts`，storage 改从 shared 导入 |
| `hooks/useIsMobile.tsx` | react | 直接拷贝 |
| `utils/loadable.tsx` | @loadable/component, antd | 直接拷贝 |
| `utils/safeMessage.ts` | antd, dompurify, lodash, i18next | 直接拷贝（i18n 用全局实例） |
| `utils/utils.ts` | 无（纯函数） | 直接拷贝为 `misc.ts` |

**4 个应用的 storage.ts / useIsMobile.tsx md5 完全一致**，零风险。

### B 类：需参数化后抽取

| 文件 | 应用依赖 | 改造方式 |
|---|---|---|
| `hooks/useDocumentTitle.ts` | `@/router/routes.config` | 改为接收 `getRoutes` 参数：`useDocumentTitle(getRoutes)` |
| `hooks/usePageMeta.ts` | 各应用 meta 逻辑 | 同上参数化 |
| `utils/menu.tsx` | `@/router/menu.config`（IconMap + MenuItem） | 接收 `{ IconMap, menuConfig }` 参数，或 IconMap 抽到 shared |

### C 类：组件骨架化（差异较大，分阶段）

| 组件 | 应用差异 | 抽取方式 |
|---|---|---|
| `BasicLayout` | 菜单来源不同（getMenuConfig 函数 / menuConfig 常量） | 抽骨架 `AppLayout`，菜单通过 props 注入 |
| `HeaderNav` | 依赖 useStore / menu / utils | 抽通用部分，差异用 props 配置 |
| `HostedLayout` / `AppLayout` / `RouteSync` | 各应用略有差异 | 逐一比对，能统一则抽 |

### D 类：API client 统一

| 应用 | 现状 | 统一方案 |
|---|---|---|
| core-business | `request.ts`（71 行，含 token/locale 拦截） | 抽 `@jonex/shared-lib/api/request.ts` |
| platform-management | `client.ts`（41 行） | 统一用 shared，差异通过配置项（baseURL 等） |
| ecosystem-management | `client.ts`（41 行） | 同上 |

统一后支持配置：`createApiClient({ baseURL, onUnauthorized })`。

## 四、迁移步骤（分阶段，每步可独立验证）

### 阶段 1：抽 A 类纯工具（风险最低）

```bash
# 1. 创建 shared-lib 包
mkdir -p shared/shared-lib/src/{utils,hooks,components,api}

# 2. 拷贝并改造 storage.ts（拆出 clearLocalStorageExcept 依赖）
# 3. 拷贝 useIsMobile / loadable / safeMessage / misc
# 4. 4 个应用 package.json 添加 "@jonex/shared-lib": "workspace:*"
# 5. 各应用改导入：@/utils/storage → @jonex/shared-lib
# 6. typecheck + 构建验证
```

**验证**：`pnpm --filter @jonex/core-business typecheck` 等 4 应用全过。

### 阶段 2：抽 B 类参数化 hooks

```bash
# useDocumentTitle(getRoutes) 改为接受路由生成函数
# 各应用调用处传自己的 getRoutes
```

### 阶段 3：ConfirmDialog 通用确认弹窗

```bash
# 新建 ConfirmDialog 组件
# 替换 7 个 DeleteConfirmModal / 确认弹窗
```

### 阶段 4：统一 API client

```bash
# 抽 createApiClient 到 shared
# core/platform/ecosystem 逐个替换，保持响应结构兼容
```

### 阶段 5（可选）：布局骨架化

```bash
# 抽 BasicLayout 骨架，菜单 props 注入
# 评估 HeaderNav / RouteSync 差异后统一
```

## 五、依赖关系图

```text
@jonex/shared-lib
  ├── utils/storage.ts ──→ misc.ts (clearLocalStorageExcept)
  ├── utils/safeMessage.ts ──→ antd, dompurify, lodash, i18next
  ├── utils/loadable.tsx ──→ @loadable/component, antd
  ├── hooks/useDocumentTitle ──→ react-router, i18next (接收 routes 参数)
  ├── components/ConfirmDialog ──→ antd, i18next
  └── api/request.ts ──→ axios, @jonex/shell-sdk

4 个子应用 ──依赖──→ @jonex/shared-lib (workspace:*)
```

## 六、风险与注意事项

1. **safeMessage 的 i18n 实例**：当前用全局 `i18next`，抽包后需确认各应用 i18n 初始化顺序，避免 `i18n.t` 在初始化前调用
2. **loadable 的 chunk 失败处理**：`window.__CHUNK_LOAD_HANDLER_INSTALLED` 全局标记，多包并存时确保只注册一次
3. **storage 依赖链**：`storage.ts` → `clearLocalStorageExcept` 需一起迁移，不能只移一半
4. **API 响应结构**：core 用 `{ code, message, data }`，platform/ecosystem 用 `{ success, message, data }` —— 统一 client 需兼容两种 envelope，或后端统一
5. **导入路径迁移**：大量 `@/utils/xxx` 引用需批量替换，用 codemod 或正则 + typecheck 兜底
6. **组件差异**：BasicLayout/HeaderNav 各应用有真实差异（菜单、store），不可强行 100% 一致，抽骨架而非整体复制

## 七、验证方式

每阶段完成后：

```bash
# 4 个应用 typecheck
pnpm -r --if-present typecheck

# 构建
pnpm run build

# 手动回归：4 个应用启动，检查布局 / 菜单 / 弹窗 / 请求正常
pnpm run dev:core-business
pnpm run dev:platform-management
pnpm run dev:ecosystem-management
```

## 八、工作量估算

| 阶段 | 内容 | 工作量 |
|---|---|---|
| 1 | A 类工具抽取 | 0.5 天 |
| 2 | B 类 hooks 参数化 | 0.5 天 |
| 3 | ConfirmDialog 统一 | 0.5 天 |
| 4 | API client 统一 | 0.5-1 天 |
| 5 | 布局骨架化 | 1-2 天（可选） |
| **合计** | | **3-5 天** |
