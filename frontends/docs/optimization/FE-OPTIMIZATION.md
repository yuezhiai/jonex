# 前端项目优化分析

> 本文档基于对悦溪平台前端代码库的分析，梳理可优化方向，按优先级排序。
> 分析时间：2026-07-31

## 项目现状

```text
frontends/  (pnpm workspace, 5 应用 + 3 共享包)
├── shell/                   21 个源文件   壳应用
├── core-business/          136 个源文件   核心业务（最大）
├── platform-management/     59 个源文件   平台管理
├── ecosystem-management/    57 个源文件   生态管理
├── _template/                           新应用模板
└── shared/
    ├── i18n-resources/                   多语言资源
    ├── platform-theme/                   主题 + 全局样式（yx-* 体系）
    └── shell-sdk/                        登录态 / 空间 / 应用清单
```

**基础设施已具备**：
- ✅ `@jonex/platform-theme`（含 yx-* 全局样式体系，24 个通用类）
- ✅ `@jonex/shell-sdk`
- ✅ `@jonex/i18n-resources`
- ✅ 后端菜单管理 API（`GET /api/v1/platform/menus`，前端未使用）
- ✅ Module Federation + 依赖 singleton 共享（react/antd）
- ✅ husky + eslint + prettier + commitlint 提交流程

---

## 一、代码重复（最大优化点）⚠️ P0

### 1.1 完全重复的文件（同 md5 校验）

| 文件 | 重复应用数 | 校验状态 |
|---|---|---|
| `utils/storage.ts` | 4（core/platform/eco/template） | ✅ 完全一致 |
| `hooks/useIsMobile.tsx` | 3（core/platform/eco） | ✅ 完全一致 |
| `utils/loadable.tsx` | 4 | 几乎一致 |
| `locales/i18n.ts` | 3 | ✅ 一致 |
| `utils/safeMessage.ts` / `utils.ts` / `menu.tsx` | 4 | 高度重复 |

### 1.2 结构重复的组件（每个应用各一套）

```
components/BasicLayout     components/AppLayout
components/HeaderNav       components/HostedLayout
components/RouteSync
hooks/useDocumentTitle     hooks/usePageMeta
```

### 1.3 API client 三套写法

| 应用 | 文件 | 行数 |
|---|---|---|
| core-business | `api/request.ts` | 71 |
| platform-management | `api/client.ts` | 41 |
| ecosystem-management | `api/client.ts` | 41 |

响应拦截、错误处理逻辑不统一。

### 1.4 重复的确认弹窗（7 个）

每个应用都有 `DeleteConfirmModal` 等确认弹窗，结构 90% 相同（标题 + 警告图标 + 确定/取消）。

### 优化方案：新建 `@jonex/shared-lib` 共享包

把通用工具 + hooks + 基础组件抽到 `frontends/shared/`：

```text
shared/shared-lib/
  ├── utils/        storage.ts, safeMessage.ts, loadable.tsx, utils.ts
  ├── hooks/        useIsMobile, useDocumentTitle, usePageMeta
  ├── components/   ConfirmDialog, HeaderNav, Layout 骨架
  └── api/          request.ts 统一封装
```

**收益**：删除 4 个应用内 20+ 重复文件，只维护一份。
**实施顺序**：从 `storage.ts` / `useIsMobile` 等完全一致的文件开始，风险最低。

---

## 二、通用组件封装 ⚠️ P1

### 2.1 确认弹窗统一

全项目 49 个 Modal 组件中，确认类（删除/启停）可抽成配置化组件：

```tsx
<ConfirmDialog
  open={open}
  title="删除确认"
  content="确定要删除 X 吗？"
  danger
  onConfirm={handleDelete}
/>
```

**收益**：7 个 `DeleteConfirmModal` 合并为 1 个组件。

### 2.2 列表页通用模式

大量页面是「搜索框 + 状态筛选 + Table + 分页 + 新建按钮」，可抽 `useListPage` hook 或 `ListPageLayout`：

```tsx
const { data, loading, search, refresh, pagination } = useListPage(fetchFn, { spaceId });
```

覆盖场景：日志管理、用户管理、租户管理、知识库文档列表、领域服务等。

### 2.3 表单 Modal 基类

已完成 Form 化的弹窗可抽一个 `FormModal` 基类，统一管理：
- open 状态
- confirmLoading
- Form 生命周期（打开填充 / 提交校验 / 关闭重置）

---

## 三、菜单 / 路由优化 ⚠️ P1

### 3.1 当前配置痛点

新增页面需配置 4 处：

| # | 位置 | 内容 |
|---|---|---|
| 1 | shell `prototypeNav.config.ts` | appId + internalPath + label |
| 2 | 子应用 `routes.config.ts` | 路由（必须） |
| 3 | 子应用 `menu.config.ts` | standalone 侧边栏 |
| 4 | i18n 文案 ×2 | shell + 子应用 |

### 3.2 优化方案

后端 `GET /api/v1/platform/menus` 已存在（Menu 模型含 `parent_id/path/icon/app_id`），接入后：

- 删除 shell `prototypeNav.config.ts` + 4 个子应用 `menu.config.ts`
- 菜单后台动态配置，加页面不用发版
- 配置点从 4 处降到 1 处（路由仍必须）

详细分析见 `docs/` 菜单优化方案。

---

## 四、工程化统一 ⚠️ P2 ✅

| 项 | 现状 | 优化 |
|---|---|---|
| ESLint | 1 份在 frontends 根（已统一） | — |
| TypeScript | 各应用独立 tsconfig | 抽 `tsconfig.base.json` 统一 ✅ |
| Vite | 3 份相似但 md5 不同 | 抽 `vite.base.config.ts` 共享 ✅ |
| 依赖管理 | 各应用独立装 antd/react | 已 Module Federation singleton 共享 |
| 构建缓存 | pnpm store 有 cache mount | 统一 `cacheDir` 到 `node_modules/.vite` ✅ |

---

## 五、性能优化 ⚠️ P2

1. **长列表虚拟滚动**：日志管理、用户列表数据量大时用 `rc-virtual-list` 或 Table 虚拟滚动
2. **图片懒加载**：上传预览、头像等
3. **组件级 code splitting**：大组件（知识图谱 G6、富文本编辑器）按需加载
4. **全局样式收敛**：页面内大量内联 style 抽成语义类（已有 yx-* 体系）

---

## 六、优先级与预期收益

| 优先级 | 事项 | 工作量 | 收益 |
|---|---|---|---|
| **P0** | 抽 `@jonex/shared-lib`（工具 + hooks） | 中 | 高（删 20+ 重复文件） |
| **P1** | ConfirmDialog / 列表页模式统一 | 中 | 高（7 个弹窗合并） |
| **P1** | 接入后端菜单 API | 中 | 高（配置 4→1 处） |
| **P2** | 统一 tsconfig / vite base | 小 | 中 |
| **P2** | 虚拟滚动 / 懒加载 | 中 | 视数据量 |

---

## 附：已完成的优化（近期）

- ✅ husky + eslint + prettier + commitlint 提交流程
- ✅ MobX → Zustand 状态管理迁移（4 个子应用）
- ✅ 弹窗 Form 表单化改造
- ✅ HTML 元素（table/button/textarea/ul）→ antd 组件
- ✅ 视频播放时间定位
- ✅ 日志字段展示优化
- ✅ 工程化统一：`tsconfig.base.json` + `vite.base.config.ts` 收敛
