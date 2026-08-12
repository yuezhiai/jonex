# Platform Management 子应用 i18n 多语言改造计划

## 项目概况

- **目录**: `frontends/platform-management/`
- **工作量**: 408 行 / 29 个文件（其中 mock 数据 40 行可跳过）
- **当前 locale keys**: 93（zh.json 和 en.json 已同步，但覆盖率仅 ~20%）
- **i18n 基础设施**: 已就绪 — `src/locales/i18n.ts` + `zh.json/en.json`
- **改造难度**: ⭐⭐⭐ 中高（文件分散、部分完成度低）

## 总体数据

| 类别 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| 🔴 完全未开始 | 16 | 172 | 含 mock 数据 40 行 + KnowledgeCompile 系列 83 行 |
| 🟡 部分已完成 | 13 | 236 | 已引入 t() 但仍有大量残留 |
| **合计** | **29** | **408** | |

---

## 文件级改造清单

### 🔴 完全未开始（16 文件 / 172 行）

#### 1. `src/data/mock.ts` — 40 行 ⚠️ 建议跳过
- **内容**: 租户列表、模型配置等 mock 数据
- **建议**: **跳过** — 纯开发/演示占位数据

#### 2-5. KnowledgeCompile 系列（4 文件 / 71 行）

##### `src/pages/KnowledgeCompile/index.tsx` — 22 行
| 内容类型 | 行数 | 说明 |
|---|---|---|
| 状态映射 | ~5 | `running: { label: '运行中' }` 等 |
| 统计卡片标题 | ~5 | `编译任务总数`、`今日新增` 等 |
| 操作按钮 | ~4 | `触发编译`、`查看详情` |
| 表格列 | ~8 | 中文列标题 |

##### `src/pages/KnowledgeCompileCompile/index.tsx` — 19 行
状态映射 + 操作按钮 + 消息提示，结构与 KnowledgeCompile 类似

##### `src/pages/KnowledgeCompileGraph/index.tsx` — 17 行
- 统计卡片: `实体节点总数`、`关系边总数`、`关系类型数` 等
- 表格数据含中文实体名称（Mock 数据）

##### `src/pages/KnowledgeCompileSearch/index.tsx` — 13 行
- 搜索结果 mock 数据（占 ~10 行）
- 页面标题 / 筛选标签

#### 6-8. 其他未开始页面（3 文件 / 21 行）

##### `src/pages/TaskSchedule/index.tsx` — 17 行
- 任务列表 mock 数据（~15 行，可跳过）
- 操作按钮、状态标签

##### `src/pages/KnowledgeCompileVector/index.tsx` — 12 行
- 搜索结果 mock 数据（~10 行，可跳过）
- 页面标题

##### `src/pages/Home/index.tsx` — 9 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L7 | `模型适配` / `管理 AI 模型与适配器` | `platform.modelAdapter` + desc |
| L8 | `租户管理` / `管理平台租户与配额` | `platform.tenantManagement` + desc |
| L9 | `用户管理` / `用户账号与权限管理` | `navigation.userManagement` + desc |
| L10 | `角色权限` / `角色定义与权限配置` | `navigation.rolePermission` + desc |
| L11 | `任务调度` / `定时任务与调度管理` | `platform.taskSchedule` + desc |

#### 9-12. 路由 & API（4 文件 / 16 行）

##### `src/router/menu.config.ts` — 10 行
所有侧栏菜单的 `label` 字段：

| 原文 | 建议 key |
|---|---|
| `首页` | `navigation.home`（已有） |
| `模型适配` | `navigation.modelAdapter`（已有） |
| `租户管理` | `navigation.tenantManagement`（已有） |
| `用户管理` | `navigation.userManagement`（已有） |
| `角色权限` | `navigation.rolePermission`（已有） |
| `审计日志` | `navigation.operationLog`（已有） |
| `系统设置` | `navigation.systemConfig`（已有） |
| `引擎管理` | `navigation.engineManagement`（已有） |
| `解析器` | `navigation.parserManagement`（已有） |
| `数据源` | `navigation.dataAccess`（已有） |

🎉 **这些全部可以直接复用共享 key！**

##### `src/api/auditLogs.ts` — 4 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L32 | `创建`、`更新`、`删除` | `common.*` |
| L33 | `登录`、`登出`、`检索`、`上传` | `auth.*` / `common.*` |
| L34 | `连接`、`断开` | `status.*` |

##### `src/api/parsers.ts` — 1 行
- `throw new Error('请求失败')` → 英文

##### `src/api/users.ts` — 3 行
- `throw new Error('请求失败')` 等错误消息

##### `src/api/tenants.ts` — 2 行
- 类型标签映射: `free: '免费版'` 等

##### `src/api/modelProviders.ts` — 1 行
- `throw new Error('请求失败')`

##### `src/api/dataAccess.ts` — 1 行
- `throw new Error('请求失败')`

##### `src/api/systemConfig.ts` — 1 行
- 函数中文名（类型字面量，可跳过）

#### 13-16. 组件 & 设置（3 文件 / 5 行）
- `src/components/HeaderNav/locale.tsx` — `cn 中文`（同 shell，语言切换标签）
- `src/pages/DataAccess/index.tsx` — 数据源类型描述（`API 接入` 等）

### 🟡 部分已完成（13 文件 / 236 行）

#### 17. `src/pages/UserManagement/index.tsx` — 45 行（最多单文件）
- 表单校验消息: `请填写用户名`、`请填写密码`
- 操作反馈: `已更新`、`已创建`、`保存失败`
- 表格列: 列标题中文
- 弹窗: 确认删除、状态切换

#### 18. `src/pages/SystemConfig/index.tsx` — 36 行
- 配置项 label: `平台名称`、`默认语言`、`会话超时` 等
- 表单字段标签、placeholder
- 错误/成功提示消息

#### 19. `src/pages/ParserManagement/index.tsx` — 31 行
- 解析器配置 label: `关键帧提取`、`分辨率限制`、`转写模型`
- 文件格式描述: `MP4/AVI/MKV/FLV 视频文件解析`
- 表单标签 / 按钮 / 提示消息

#### 20. `src/pages/ModelAdapter/index.tsx` — 30 行
- 模型适配器 CRUD 操作提示
- 表单校验: `请填写名称`、`请选择类型`
- 操作反馈: `已更新`、`已创建`、`保存失败`

#### 21. `src/pages/TenantManagement/index.tsx` — 25 行
- 租户 CRUD 操作提示
- 表单校验: `请填写租户 ID`、`请填写租户名称`
- 状态标签、确认弹窗

#### 22. `src/pages/OperationLog/index.tsx` — 22 行
- 操作类型映射: `create: { label: '创建' }`, `update: { label: '更新' }` 等
- 表格列标题、筛选条件

#### 23. `src/pages/RolePermission/index.tsx` — 17 行
- 角色管理: `请输入角色名`、`添加角色`
- 权限设置: `加载权限失败`、`保存失败`
- 权限标签

#### 24. `src/router/routes.config.ts` — 15 行
路由配置页面标题：
| 原文 | 建议 key |
|---|---|
| `平台管理` | `route.platformManagement` |
| `模型适配` | `navigation.modelAdapter` |
| `租户管理` | `navigation.tenantManagement` |
| `用户管理` | `navigation.userManagement` |
| 等 | 复用共享 key |

#### 25. `src/pages/DataAccess/index.tsx` — 9 行
数据源类型描述:
| 原文 | 建议 key |
|---|---|
| `API 接入` | `dataSource.apiAccess` |
| `API 开放（推送）` | `dataSource.apiPush` |
| `文件存储直连` | `dataSource.storageDirect` |
| `文件上传` | `dataSource.fileUpload` |
| `MQTT 接入` | `dataSource.mqttAccess` |

---

## 改造关键决策

### 1. 复用共享 i18n-resources

platform-management 的菜单配置和大部分路由标题已有 **共享 key 可直接复用**，无需新增：

```typescript
// 改造前
{ key: 'home', path: '/home', label: '首页' }

// 改造后
{ key: 'home', path: '/home', label: t('navigation.home') }
```

### 2. KnowledgeCompile 系列复用 core-business

KnowledgeCompile 系列页面与 core-business 的 DomainKnowledge* 页面高度重合：
- 状态: `运行中`、`已完成`、`失败`、`等待中`
- 实体类型: `实体节点`、`关系边`、`关系类型`
- 操作: `触发编译`、`查看详情`

建议复用 `status.*` 和 `domainKnowledge.*` 命名空间的 key，避免重复造轮。

### 3. CRUD 操作消息 vs Common

大量操作反馈消息（保存成功/失败、已更新/已创建）应先检查 `common.*` 命名空间是否已有：
- `common.saveSuccess` / `common.saveFailed`
- `common.deleteSuccess` / `common.deleteFailed`
- `common.operationSuccessful` / `common.operationFailed`

### 4. mock 数据跳过

`data/mock.ts`（40 行）+ 各页面的 mock 表格数据（~30 行）= **~70 行可跳过**，占总量 17%。

---

## 改造分组建议

### 组 1: KnowledgeCompile 系列（~70 行）
- `pages/KnowledgeCompile/index.tsx`（22 行）
- `pages/KnowledgeCompileCompile/index.tsx`（19 行）
- `pages/KnowledgeCompileGraph/index.tsx`（17 行）
- `pages/KnowledgeCompileSearch/index.tsx`（13 行）
- `pages/KnowledgeCompileVector/index.tsx`（12 行）

### 组 2: 平台管理页面（~120 行）
- `pages/UserManagement/index.tsx`（45 行）
- `pages/SystemConfig/index.tsx`（36 行）
- `pages/TenantManagement/index.tsx`（25 行）
- `pages/OperationLog/index.tsx`（22 行）

### 组 3: 引擎 & 适配器（~80 行）
- `pages/ParserManagement/index.tsx`（31 行）
- `pages/ModelAdapter/index.tsx`（30 行）
- `pages/RolePermission/index.tsx`（17 行）
- `pages/TaskSchedule/index.tsx`（~5 行非 mock）

### 组 4: 基础设施 + 剩余（~70 行）
- `router/menu.config.ts`（10 行 — 全复用共享 key）
- `router/routes.config.ts`（15 行）
- `api/*` 系列（~15 行）
- `pages/Home/index.tsx`（9 行）
- `pages/DataAccess/index.tsx`（9 行）
- `components/*` 系列（5 行）

---

## 预计新增 locale keys

| 命名空间 | key 数 | 说明 |
|---|---|---|
| `platform.*` | ~25 | 平台管理特有页面文案 |
| `knowledgeCompile.*` | ~20 | 编译引擎页面 |
| `parserManagement.*` | ~15 | 解析器管理 |
| `modelAdapter.*` | ~10 | 模型适配器 |
| `dataSource.*` | ~5 | 数据源类型描述 |
| 扩充 `status.*` | ~5 | 扩展状态值 |
| 扩充 `common.*` | ~10 | 操作消息补充 |

**合计新增约 90 个 key，总 key 数从 93 增至 ~183。**

---

## 改造流程

```
Phase 1: 更新共享 locale key（新增所有必要的 namespace 和 key）
Phase 2: 并行 Agent 替换（4 组）
Phase 3: 手动修复 agent 遗漏（预计 ~20 行，主要在各 api/*.ts）
Phase 4: 验证（typecheck + 残留扫描 + locale 同步检查）
```

预估总耗时：**1.5-2 小时**

---

## ⚠️ 语言切换事件（改造前必读）

改造前需确保 `App.tsx` 和 `remote/RemoteApp.tsx` 包含 `jonex:locale-change` 事件监听（已修复）。

参考 `frontends/core-business/src/App.tsx` 的模式：
1. 使用 `I18nextProvider i18n={i18n}` 包裹根组件
2. 添加 `GlobalLocaleListener` 监听语言切换事件
3. 添加 `AntdGate` 实时同步 Ant Design 组件 locale

---

## 🔧 关键修复：i18n 实例创建方式

改造 **ecosystem-management** 过程中发现**首个页面能正常切换语言，但导航到新页面后语言重置为中文**。

### 根因：Storage Key 不一致 + 缺少共享资源

1. Shell 使用 `jonex_locale` key 保存语言到 localStorage，但子应用 i18n.ts 从 `locale` 读取（**不同 key**）
2. i18n.ts 使用 `i18next.use(initReactI18next).init({...})` 创建独立实例，**没有加载** `@jonex/i18n-resources` 的共享 locale（`common.*`、`navigation.*`、`auth.*` 等）
3. 切换到英文时，共享 key 找不到英文翻译 → fallback 到中文 → **看起来语言没切换**

### 修复方式

**`src/locales/i18n.ts`** 改用 `createI18nInstance()`：
```tsx
import { createI18nInstance } from '@jonex/i18n-resources'
import zhLocales from '@/locales/zh.json'
import enLocales from '@/locales/en.json'

const i18n = createI18nInstance({
  resources: {
    zh: { translation: zhLocales },
    en: { translation: enLocales },
  },
  // ✅ 自动 deep merge 共享资源 + 从 jonex_locale key 读取
})
```

**`package.json`** 添加依赖：
```json
"@jonex/i18n-resources": "workspace:*",
```

### 改造前检查清单
- [ ] `i18n.ts` 使用了 `createI18nInstance()` 而非手写 `i18n.init()`？
- [ ] `package.json` 已添加 `@jonex/i18n-resources` 依赖？
- [ ] `App.tsx` 和 `remote/RemoteApp.tsx` 已添加 `GlobalLocaleListener`？
