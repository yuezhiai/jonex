# Ecosystem Management 子应用 i18n 多语言改造计划

## 项目概况

- **目录**: `frontends/ecosystem-management/`
- **工作量**: 463 行 / 23 个文件（其中 mock 数据 54 行可跳过）
- **当前 locale keys**: 134（zh.json 和 en.json 已同步）
- **i18n 基础设施**: 已就绪 — `src/locales/i18n.ts` + `zh.json/en.json`
- **改造难度**: ⭐⭐⭐ 中高（量大但文件集中）
- **改造状态**: ✅ 已完成
- **最终 locale keys**: 413（zh.json 和 en.json 完全同步）
- **TypeScript 编译**: ✅ 通过

## 总体数据

| 类别 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| 🔴 完全未开始 | 6 | 74 | 含 mock 数据 54 行 |
| 🟡 部分已完成 | 17 | 389 | 已引入 t() 但仍有大量残留 |
| **合计** | **23** | **463** | |

---

## 文件级改造清单

### 🔴 完全未开始（6 文件 / 74 行）

#### 1. `src/data/mock.ts` — 54 行 ⚠️ 可跳过
- **内容**: 适配器列表 mock 数据（含大量中文名称/类型/描述）
- **建议**: **跳过** — mock 数据属于开发/演示占位，不纳入 i18n
- 如需保留: 将 mock 数据中的中文提取为 t() 调用，但元数据（如名称、类型）建议保持原样

#### 2. `src/api/adapters.ts` — 7 行
| 行号 | 原文 | 建议 key | 说明 |
|---|---|---|---|
| L35-37 | `钉钉`、`企业微信`、`飞书` | `adapters.dingtalk` 等 | 类型名称映射 |
| L41 | `已连接` | `status.connected` | 状态标签 |
| L42 | `未连接` | `status.disconnected` | 状态标签 |

#### 3. `src/pages/Home/index.tsx` — 5 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L7 | `适配器列表` / `生态适配器管理与配置` | `navigation.adapterList` + `adapters.managementDesc` |
| L8 | `业务领域商场` / `领域模板...` | `ecosystem.marketplace` |
| L16 | `<h1>生态管理</h1>` | route / home 命名空间 |
| L17 | 页面副标题 | 按需提取 |

#### 4. `src/router/menu.config.ts` — 5 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L7 | `集成适配器` | `navigation.adapterList`（已有） |
| L8 | `业务商场` | `ecosystem.marketplace` |
| L9 | `技能管理` | `navigation.skills` |
| L10 | `提示词模板` | `navigation.promptTemplates` |
| L11 | `领域模板` | `navigation.templateDomains` |

#### 5. `src/components/HeaderNav/locale.tsx` — 2 行
| 行号 | 原文 | 说明 |
|---|---|---|
| L17 | `cn 中文` | 语言切换标签 |
| L32 | `cn 中文` / `us English` | 回退显示 |

#### 6. `src/api/skills.ts` — 1 行
| 行号 | 原文 | 建议处理 |
|---|---|---|
| L50 | `throw new Error('请求失败')` | 改为英文 `Request failed` |

### 🟡 部分已完成（17 文件 / 389 行）

#### 7. `src/pages/TemplateScenarios/index.tsx` — 159 行（最多）
这是改造工作量最大的文件。内容分类：
- **属性类型选择器** (L81-90): `{ label: '字符串', value: '字符串' }` 等 — 对应 `compile.attrType.*`
- **状态映射** (L29-40): `active: { label: '启用', cls: 'active' }` 等
- **表格列定义** (L50-100): 多个列包含中文 title
- **消息提示**: `message.success('保存成功')`、`message.error('加载失败')` 等
- **表单标签**: 字段名、placeholder、校验提示

**建议拆分策略**:
- 属性类型 → 复用 `compile.attrType.*`
- 状态 → 复用 `status.*`
- 表格标题 → `templateScenarios.*`
- 操作消息 → `common.*`

#### 8. `src/pages/TemplateDomains/index.tsx` — 37 行
与 TemplateScenarios 类似，包含:
- 状态筛选 + 状态映射 (`启用/停用`)
- 表格列中文标题
- 操作消息提示
- 弹窗确认文本

#### 9. `src/pages/Skills/index.tsx` — 34 行
- 技能类型映射: `image: '图像处理'`, `voice: '语音处理'` 等
- 表格列标题
- 状态标签
- 操作按钮、提示消息

#### 10. `src/pages/PromptTemplates/` 系列 — ~90 行（4 个文件）
- `VersionModal.tsx` (28 行): 版本历史、回滚操作、复制提示词
- `CreateEditModal.tsx` (27 行): 新建/编辑/查看模式标题、表单字段、校验消息
- `index.tsx` (23 行): 列表页、操作按钮、加载/失败状态
- `PromptCard.tsx` (20 行): 卡片展示、复制、状态徽标、空描述

#### 11. `src/pages/TemplateObjects/index.tsx` — 15 行
对象管理页: 表格列 title、状态映射、操作按钮

#### 12. `src/pages/TemplateRelations/index.tsx` — 9 行
关系管理页: 表格列 title

#### 13. `src/router/routes.config.ts` — 9 行
路由配置中的页面 title:
| 行号 | 原文 | 建议 key |
|---|---|---|
| L23 | `生态管理` | `route.ecosystemManagement` |
| L24 | `集成适配器` | `navigation.adapterList` |
| L25 | `业务商场` | 需新增 |
| L26 | `技能管理` | `navigation.skills` |
| L27 | `领域模板` | `navigation.templateDomains` |

#### 14. 其他文件（~20 行）
- `src/pages/AdapterManagement/index.tsx` — 适配器管理页表格/按钮
- `src/pages/BusinessMarketplace/index.tsx` — 商场页面文案
- `src/components/HeaderNav/index.tsx` — 导航栏文案
- `src/api/*` — 少量 throw Error 消息

---

## 改造关键决策

### 1. 属性类型映射复用

core-business 中 `compile/constants.ts` 已有一套完整的属性类型映射。建议 **复用相同 key 命名**：

```typescript
// 改造前
{ label: '字符串', value: '字符串' }

// 改造后  
{ label: t('compile.attrType.string'), value: '字符串' }
```

### 2. 状态映射复用

优先复用共享 i18n-resources 或 core-business 中已有的状态 key：

| 状态 | 建议 key |
|---|---|
| 启用 | `status.enabled`（共享已有） |
| 停用 | `status.inactive`（共享已有） |
| 草稿 | `status.draft`（共享已有） |
| 运行中 | `status.running`（需新增） |
| 已连接 | `status.connected`（需新增） |
| 未连接 | `status.disconnected`（需新增） |

### 3. 开发 vs 生产问题

**mock 数据** (54 行) 和 **demo 占位文本** 建议标记 `/* @i18n-mock */` 注释暂缓翻译，降低工作量约 15%。

---

## 改造分组建议

建议分 4 组并行处理，每组建模约 100 行：

### 组 1: TemplateScenarios（~159 行）
- `pages/TemplateScenarios/index.tsx`
- 预计需要 1-2 轮 agent 处理（量最大，可分批走）

### 组 2: PromptTemplates 系列（~90 行）
- `pages/PromptTemplates/index.tsx`
- `pages/PromptTemplates/CreateEditModal.tsx`
- `pages/PromptTemplates/VersionModal.tsx`
- `pages/PromptTemplates/PromptCard.tsx`

### 组 3: 领域模板 + 技能（~90 行）
- `pages/TemplateDomains/index.tsx`（37 行）
- `pages/Skills/index.tsx`（34 行）
- `pages/TemplateObjects/index.tsx`（15 行）
- `pages/TemplateRelations/index.tsx`（9 行）

### 组 4: 剩余 + 基础设施（~80 行）
- `router/menu.config.ts` + `router/routes.config.ts`
- `api/*` 系列
- `components/*` 系列
- `pages/Home/index.tsx`
- `pages/AdapterManagement/index.tsx`
- `pages/BusinessMarketplace/index.tsx`

---

## 预计新增 locale keys

| 命名空间 | key 数 | 说明 |
|---|---|---|
| `ecosystem.*` | ~20 | 生态管理页面特有文案 |
| `templateScenarios.*` | ~30 | 模板场景页面 |
| `templateDomains.*` | ~15 | 领域模板 |
| `skills.*` | ~15 | 技能页面 |
| `promptTemplates.*` | ~20 | 提示词模板 |
| `status.*` | ~5 | 扩展状态 |
| 扩充 `common.*` | ~10 | 通用操作消息 |

**合计新增约 115 个 key，总 key 数从 134 增至 ~250。**

---

## 改造流程

```
Phase 1: 准备 locale key（统一添加所有新 key 到 zh.json/en.json）
Phase 2: 并行 Agent 替换（4 组并行，每组 ~100 行）
Phase 3: 手动修复 agent 遗漏（预计 ~20 行）
Phase 4: 验证（typecheck + 残留扫描 + locale 同步检查）
```

预估总耗时：**1.5-2 小时**（4 agent 并行，~20 min/轮 typecheck）

---

## ⚠️ 语言切换事件修复

改造完成后发现切换语言后界面不刷新，原因是没有监听 Shell 分发的 `jonex:locale-change` 事件。

### 修复文件

| 文件 | 修改内容 |
|---|---|
| `src/App.tsx`（standalone 模式） | 添加 `I18nextProvider` + `GlobalLocaleListener` + `AntdGate` |
| `src/remote/RemoteApp.tsx`（hosted 模式） | 同上 |

### 修复原理

Shell 的 `LocaleSwitcher` 切换语言时会派发 `jonex:locale-change` CustomEvent。子应用通过监听该事件，在自己的 i18n 实例上调用 `changeLanguage()` 实现实时切换。`AntdGate` 组件通过 `useTranslation()` 实时读取当前语言，同步更新 Ant Design 组件库的 locale。

> **注意**: 所有子应用（ecosystem-management、platform-management、shell）都已包含此修复。新建子应用时需参考 core-business 的 `App.tsx` 和 `RemoteApp.tsx` 模板。

---

## 🔧 关键修复：i18n 实例创建方式

改造后发现**首个页面能正常切换语言，但导航到新页面后语言重置为中文**。排查发现两个问题：

### 问题 1：Storage Key 不一致

Shell 切换语言时使用 `jonex_locale` key 保存到 localStorage，但子应用 i18n.ts 使用 `getItem('locale')` 从不同的 key 读取：

| 组件 | 写入 key | 读取 key |
|---|---|---|
| Shell `LocaleSwitcher` | `jonex_locale` ✅ | — |
| 旧 `i18n.ts` | — | `locale` ❌ |
| 旧 `store/global.ts` | `locale` ❌ | `locale` ❌ |

### 问题 2：缺少共享语言资源

旧代码直接用 `i18next.use(initReactI18next).init({...})` 创建独立 i18n 实例，**仅加载了本项目自己的 locale JSON**。但菜单配置、导航标题等大量使用了来自 `@jonex/i18n-resources` 的共享 key（`common.*`、`navigation.*`、`auth.*` 等），这些 key 不在本项目 locale 文件中。

切换语言到英文时，这些共享 key 找不到英文翻译，fallback 到中文，导致**看起来语言没切换**。

### 修复方案

```tsx
// 旧代码：创建独立 i18n 实例，不加载共享资源
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhLocales from '@/locales/zh.json'
import enLocales from '@/locales/en.json'
import { getItem } from '@/utils/storage'

i18n.use(initReactI18next).init({
  lng: (getItem('locale') as string) || 'zh',  // ❌ 读取错误 key
  resources: { zh: { translation: zhLocales }, en: { translation: enLocales } },
  // ❌ 缺少 shared common/auth/navigation/* 资源
})

// 新代码：使用 createI18nInstance 自动合并共享+业务资源
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

### 修复文件清单

| 文件 | 修改 |
|---|---|
| `src/locales/i18n.ts` | 改用 `createI18nInstance()` 替代手写 init |
| `package.json` | 添加 `@jonex/i18n-resources: "workspace:*"` 依赖 |

> ⚠️ **template 注意事项**: 新建子应用时，`i18n.ts` 必须用 `createI18nInstance()` 创建实例，**不要**直接 `new i18n`。<br>
> 使用 `createI18nInstance()` 的好处：<br>
> 1. 自动加载共享 locale 资源（common、auth、navigation 等）<br>
> 2. 从统一的 `jonex_locale` storage key 读取语言设置<br>
> 3. deepMergeTranslations 自动合并不同来源的资源
