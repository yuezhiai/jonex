# core-business 国际化改造方案

> 版本：v1.0  
> 依据文档：
> - `docs/open-source-migration-plans/frontend-i18n-brand-open-source-plan.md`（主计划）
> - `frontends/i18n-status.md`（当前状态）
> - `frontends/shared/i18n-resources/`（共享基础设施）
> - `frontends/core-business` 代码实际现状
> - `frontends/core-business-i18n-execution-plan.md`（执行步骤）

---

## 1. 现状分析

### 1.1 已完成项

根据 `frontend-i18n-brand-open-source-plan.md` 各阶段对照：

| 阶段 | 条目 | 现状 |
|---|---|---|
| §6 共享基础设施 | `@jonex/i18n-resources` 包 | ✅ 已建立 |
| §6 B3 | `i18n:check` 校验脚本 | ✅ 已建立 |
| §7 C1 Shell 语言状态源 | `jonex:locale-change` 事件 | ✅ Shell 已实现 |
| §7 C2 ShellContext.locale | 类型化 locale 传入远程应用 | ✅ Shell 已实现 |
| §7 C3 Ant Design locale 同步 | zhCN/enUS 切换 | ✅ Shell 已实现 |
| **§8 子应用接入** | **core-business 接入共享基础设施** | **❌ 未完成** |

### 1.2 当前代码缺口

#### 基础设施缺口

| 检查项 | 期望（主计划 §8） | 实际 | 严重性 |
|---|---|---|---|
| 依赖 `@jonex/i18n-resources` | package.json 声明 | ❌ 未声明 | 阻塞 |
| `i18n.ts` 使用 `createI18nInstance()` | 调用共享工厂 | ❌ 独立 i18n 实例 | 阻塞 |
| `fallbackLng` | `'en'`（§6 B1） | ❌ `'zh'` | 高 |
| localStorage key | `'jonex_locale'`（常量） | ❌ `'locale'`（散落字符串） | 高 |
| 监听 `jonex:locale-change` | RemoteApp 监听事件 | ❌ 未实现 | 高 |
| 语言切换入口 | 仅 Shell 提供（§7 C2） | ❌ 保留 `HeaderNav/locale.tsx` | 中 |
| Ant Design locale 同步 | 来自 ShellContext 或事件 | ❌ 从 localStorage 读取 | 高 |
| `I18nextProvider` | 包裹 App | ❌ 未使用（直接 `i18n.init()`） | 中 |

#### 翻译资源文件缺口

| 检查项 | 期望 | 实际 |
|---|---|---|
| 业务命名空间 | `domainKnowledge.*`、`domainSpace.*`、`knowledgeSearch.*` 等 | ❌ 不存在 |
| 共享 key 去重 | 仅保留业务专属 key（§6 B2） | ❌ `zh.json`/`en.json` 含大量与共享包重复的 `common.*`、`auth.*`、`error.*` |
| `en.json` 完整度 | 英文为默认语言 | ❌ 仅 113 条 key，无业务 key |

#### 运行时硬编码缺口

按 `frontend-i18n-brand-open-source-plan.md` §9 标准扫描：

| 分类 | 涉及文件 | 中文行数 | 对应 §E 子章节 |
|---|---|---|---|
| 文件上传状态链 | `api/dataSource.ts`、`api/domainKnowledge.ts` | ~26 | §E.4 业务值分离 |
| 枚举/类型映射 | `types/domainKnowledge.ts`、`api/domainKnowledge.ts` | ~22 | §E.4 |
| 编译常量映射 | `compile/constants.ts` | 34 | §E.1 §3 |
| Table 列名/emptyText | `DomainSpace/index.tsx`、`DomainManagement/index.tsx` 等 | ~60 | §E.1 §3 |
| Form label/placeholder | `SpaceFormModal.tsx`、`DomainKnowledgeParser/index.tsx` | ~20 | §E.1 §3 |
| 弹窗/确认文案 | `DomainSpace/index.tsx` 权限弹窗等 | ~30 | §E.1 §3 |
| 错误/加载/重试 | `api/request.ts`、`DomainSpace/index.tsx` | ~10 | §E.1 §4 |
| 首页/空间管理列名 | `Home/index.tsx`、`DomainSpace/index.tsx` | ~50 | §E.1 §2-3 |
| 图谱/源数据/引擎 | `DomainKnowledgeGraph/index.tsx` 等 | ~50 | §E.1 §3 |
| 编译页面表单/标签 | `compile/*.tsx` 各文件 | ~30 | §E.1 §3 |
| Mock 演示数据 | `data/mock.ts`、`types/domainService.ts` | ~70 | §E.1 §6 |
| 其他零散 | 权限/设置/搜索/编译结果页面 | ~50 | §E.1 §3-4 |

> 注：emptyText 集中分布于 15+ 个 Table，是替换性价比最高的类别

---

## 2. 总方案

### 2.1 改造路径

```text
Phase 1 ─── 基础设施接入（§8 子应用接入标准）
  ├── 1.1 package.json 添加依赖
  ├── 1.2 重写 i18n.ts 使用共享工厂
  ├── 1.3 main.tsx + App.tsx 改造
  ├── 1.4 接入 locale-change 事件监听
  ├── 1.5 删除独立语言切换器
  └── 1.6 简化本地 locale 文件（去重共享 key）

Phase 2 ─── 业务翻译资源补充（§6 B2 资源所有权）
  ├── 2.1 补充业务命名空间 zh.json
  ├── 2.2 补充业务命名空间 en.json
  └── 2.3 i18n:check 校验

Phase 3 ─── 运行时硬编码替换（§9 阶段 E）
  ├── 3.1 业务值与显示标签分离（§E.4）
  ├── 3.2 Table columns/emptyText（批量化，性价比最高）
  ├── 3.3 表单/弹窗/确认文案（按页面）
  ├── 3.4 错误/加载/空状态
  ├── 3.5 编译页面（复杂区域）
  └── 3.6 Mock 数据英文化（§G.3）

Phase 4 ─── 验收（§12 测试与验收矩阵）
  ├── 4.1 自动门禁
  └── 4.2 双语言冒烟
```

### 2.2 技术架构变更

```text
改造前：
  core-business/src/locales/i18n.ts ── 独立 i18n 实例
  core-business/src/locales/zh.json ── 含重复 common key
  core-business/src/locales/en.json ── 无业务 key
  App.tsx ── localStorage 读 locale + 本地 Ant Design locale
  HeaderNav/locale.tsx ── 独立语言切换按钮
  → 不与 Shell 同步

改造后：
  @jonex/i18n-resources (shared) ─── createI18nInstance()
    │                                    ↓
    │                     core-business/src/locales/i18n.ts
    │                       └─ 调用工厂，注入业务资源
    │
  Shell ── jonex:locale-change ──────→ RemoteApp 事件监听
    │                                    ↓
    │                     core-business 更新 i18n language
    │                                 更新 Ant Design locale
    │
  ShellContext.locale ── hosted 初始值
  localStorage('jonex_locale') ── standalone 初始值
```

---

## 3. 详细步骤

### Phase 1：基础设施接入

#### 步骤 1.1：package.json 添加依赖

**做什么**：在 `core-business/package.json` 的 `dependencies` 中添加 `@jonex/i18n-resources: "workspace:*"`，并确保 `i18next` 和 `react-i18next` 版本与共享包一致。

**对应主计划**：§8 §D.1 — `package.json` 使用 `@jonex/*` scope，声明共享资源包

**验收**：`pnpm install --frozen-lockfile` 成功，`pnpm --filter @jonex/core-business typecheck` 通过

---

#### 步骤 1.2：重写 i18n.ts

**做什么**：将 `src/locales/i18n.ts` 从独立初始化改为调用 `createI18nInstance()` 工厂。

**当前代码**：
```typescript
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhLocales from '@/locales/zh.json'
import enLocales from '@/locales/en.json'
import { getItem } from '@/utils/storage'

i18n.use(initReactI18next).init({
  fallbackLng: 'zh',
  lng: (getItem('locale') as string) || 'zh',
  resources: { zh: { translation: zhLocales }, en: { translation: enLocales } },
  interpolation: { escapeValue: false },
})
export default i18n
```

**目标代码**：
```typescript
import { createI18nInstance } from '@jonex/i18n-resources'
import zhLocales from '@/locales/zh.json'
import enLocales from '@/locales/en.json'

const i18n = createI18nInstance({
  resources: {
    zh: { translation: zhLocales },
    en: { translation: enLocales },
  },
})
export default i18n
```

**需要确认的点**：
- 工厂内部的 `normalizeLocale()` 会从 `localStorage('jonex_locale')` 读取初始语言
- `fallbackLng` 默认为 `'en'`（由工厂决定）
- 独立模式（standalone）和 hosted 模式的初始语言读取需要核对：
  - Standalone：由 `normalizeLocale()` 内部读取 localStorage 即可
  - Hosted：需要等待 ShellContext 传入，i18n.ts 初始化时不能直接通过 `createI18nInstance({ lng })` 传递

**对应主计划**：§8 §D.2 — `src/locales/i18n.ts` 调用 `createI18nInstance()`，注入业务资源；§6 B1 — 工厂内部逻辑

**验收**：`typecheck` 通过，`createI18nInstance()` 正确合并共享 + 业务资源

---

#### 步骤 1.3：改造 main.tsx 和 App.tsx

**做什么**：

**main.tsx**：
- 使用 `I18nextProvider` 包裹子应用根组件（如果需要）
- hosted 模式初始语言来自 `ShellContext.locale`
- standalone 模式初始语言来自 localStorage（由 `normalizeLocale` 处理）

**App.tsx**：
- 当前从 `localStorage.getItem('locale')` 读取 Ant Design locale
- 改为从 `useTranslation()` 的 `i18n.language` 获取当前语言
- 或监听 `i18n` 实例的 `languageChanged` 事件更新 Ant Design locale
- 移除直接 `localStorage.getItem('locale')` 引用

```typescript
// App.tsx 改造示例
import { useTranslation } from 'react-i18next'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'

function App() {
  const { i18n } = useTranslation()
  const antdLocale = i18n.language === 'zh' ? zhCN : enUS

  return <ConfigProvider locale={antdLocale} theme={antdTheme}>...</ConfigProvider>
}
```

**对应主计划**：§8 §D.3 — `main.tsx` 使用 `I18nextProvider`；§8 §D.5 — `ConfigProvider` locale 与 i18n language 同步

**验收**：`typecheck` 通过，应用正常运行，Ant Design 组件跟随语言切换

---

#### 步骤 1.4：加入 locale-change 事件监听

**做什么**：在 `RemoteApp.tsx` 或根组件中加入 `jonex:locale-change` 事件监听，用于 hosted 模式下接收 Shell 的语言切换通知。

```typescript
useEffect(() => {
  const handler = (e: CustomEvent<string>) => {
    i18n.changeLanguage(e.detail)
  }
  window.addEventListener('jonex:locale-change', handler as EventListener)
  return () => window.removeEventListener('jonex:locale-change', handler as EventListener)
}, [])
```

**关键细节**：
- hosted 模式需要此监听，standalone 不需要（但加上了也不影响）
- cleanup 时解绑，防止重复 mount 导致多次更新（§7 C2）
- 本应用不派发 `jonex:locale-change`，仅 Shell 派发（§7 C1）

**对应主计划**：§7 C1 — Shell 派发 CustomEvent；§8 §D.4 — RemoteApp 监听事件

**验收**：Shell 切换语言后，core-business 所有组件刷新为对应语言

---

#### 步骤 1.5：删除独立语言切换器

**做什么**：
- 删除 `src/components/HeaderNav/locale.tsx`
- 检查 `HeaderNav/index.tsx` 中对 locale 的引用，移除语言切换按钮
- 清理 `global.setLocale()` 和 `global.locale` 相关代码

**对应主计划**：§7 C2 — hosted 模式不显示子应用自己的语言切换器；§8 §D.7 — 删除各应用独立语言切换器

**验收**：`typecheck` 通过，HeaderNav 不再显示语言切换按钮

---

#### 步骤 1.6：简化本地 locale 文件，去重共享 key

**做什么**：
移除 `zh.json` / `en.json` 中与共享包 `@jonex/i18n-resources/src/locales/{zh,en}.json` 重复的 key。

**需要保留的**（共享包没有的 key）：
- `auth.*`——共享包中 `auth.*` 已存在，但本应用的 `auth.signIn`、`auth.signInSSO`、`auth.forgotPassword` 等是业务登录页追加的
- `reset.*`——共享包无此命名空间
- 所有未来新增的业务 key

**需要移除的**（共享包已包含的 key）：
- `common.search`、`common.cancel`、`common.save` 等通用 CRUD key
- `error.404`、`error.network`、`error.500` 等通用错误
- `status.enabled`、`status.active` 等状态 key
- `language.*`
- `site.*`
- `rules.*`（共享包已有但键名格式不同，需统一）

**注意**：由于 `createI18nInstance()` 内部实现中，`deepMergeTranslations` 会合并共享资源和业务资源，如果业务资源和共享资源都定义了同名 key，业务资源的优先级更高。因此移除重复 key 不是必须的（业务资源会覆盖共享资源），但为了精简建议做。

**对应主计划**：§6 B2 — 共享包只保留跨两个以上应用复用的词条，业务应用禁止复制一整套相同 `common` 块

**验收**：`typecheck` 通过，`i18n:check` 通过，语言文件 size 减少

---

### Phase 2：业务翻译资源补充

#### 步骤 2.1：补充 zh.json 业务命名空间

**做什么**：为 `zh.json` 添加以下业务命名空间的中文翻译。这是后续所有 `t('xxx.yyy')` 调用能正确显示的**前置条件**。

**预计新增的命名空间和 key**：

| 命名空间 | 用途 | 预估数量 |
|---|---|---|
| `domainSpace.*` | 空间管理：列名、按钮、弹窗、权限 | ~25 |
| `domainKnowledge.*` | 知识库：列表、详情、Tab、操作 | ~30 |
| `knowledgeSearch.*` | 知识检索页面 | ~15 |
| `domainService.*` | 领域服务页面 | ~15 |
| `dataSource.*` | 数据源管理 | ~20 |
| `domainEngine.*` | 引擎管理 | ~10 |
| `domainGraph.*` | 知识图谱页 | ~15 |
| `compile.*` | 编译配置：对象、关系、属性、步骤 | ~30 |
| `synonym.*` | 同义词管理 | ~10 |
| `ontology.*` | 本体编辑 | ~20 |
| `permission.*` | 权限角色 | ~8 |
| `knowledgeTracking.*` | 知识跟踪 | ~5 |
| `documentViewer.*` | 文档查看器 | ~8 |
| `home.*` | 首页统计和欢迎 | ~10 |
| `sync.*` | 数据同步 | ~8 |
| `domainConfig.*` | 领域配置 | ~5 |
| `parserConfig.*` | 解析器配置 | ~10 |

**对应主计划**：§6 B2 — 业务应用使用模块前缀

---

#### 步骤 2.2：补充 en.json 业务命名空间

**做什么**：将步骤 2.1 中新增的中文 key 逐条翻译为英文，写入 `en.json`。

**关键约束**：
- key 集合必须与 `zh.json` 完全一致（§1 完成定义）
- 键名不是从中文翻译，而是按功能命名（如 `domainSpace.create`）
- 插值变量 `{{name}}` 在两种语言中必须一致
- 英文为默认/fallback 语言，所有 key 必须有值

**对应主计划**：§1 — 默认语言为英文；§6 B3 — 校验脚本保证 key 集合一致

---

#### 步骤 2.3：运行 i18n:check 校验

**做什么**：
```bash
cd frontends
pnpm run i18n:check
```
校验以下项目：
- JSON 合法且无重复键
- 同一资源对的 key 集合一致（zh vs en）
- 同一个 key 在两种语言中节点类型一致（不出现一个语言是字符串另一个是对象）
- `{{name}}` 等插值变量集合一致
- 缺失资源文件、空 key、异常数组时报错

**对应主计划**：§6 B3 — 自动校验脚本；§12.1 — 自动门禁

**验收**：`i18n:check` 零错误

---

### Phase 3：运行时硬编码替换

#### 步骤 3.1：业务值与显示标签分离

**做什么**：
这是最基础的工作，被多个页面引用，必须在具体页面替换**之前**完成。

**涉及文件及改造**：

| 文件 | 改造内容 |
|---|---|
| `types/domainKnowledge.ts:132` | `ValidationSeverity = '高' \| '中' \| '低'` — 保留类型不变，新增 `severityLabel: Record<ValidationSeverity, string>` 映射到 translation key |
| `types/domainKnowledge.ts:558` | `OntologyAttrType = '字符串' \| ...` — 同上 |
| `types/domainKnowledge.ts:578` | `OntologyRelationType = '一对一' \| ...` — 同上 |
| `types/domainKnowledge.ts:647-655` | 约束条件中文值 — 改为 key 映射 |
| `api/domainKnowledge.ts:616-621` | `attrTypeLabel()` 映射 `string → '字符串'` → 返回 `ontology.attrType.string` 等 key |
| `api/domainKnowledge.ts:628-633` | `attrTypeReverse()` 逆映射 → 保留稳定值不变 |
| `api/domainKnowledge.ts:640-655` | cardinality 映射 → 类似处理 |
| `api/domainKnowledge.ts:417` | 状态文字 `ready → '入库·解析·编译'` → 改为返回 `status.ready` 等 key |
| `api/dataSource.ts:118` | 同上状态文字 → 改为返回 key |
| `components/datasource/DataSourceDocTable.tsx:21` | `includes('中')` → 判断英文 code `'parsing'` |
| `pages/DomainKnowledgeDatasourceManual/index.tsx:181` | 同上 |
| `compile/constants.ts` | 34 行编译常量中文映射 → 改为 key 映射 |

**对应主计划**：§E.4 — 业务值与显示标签分离；§2.3 — 编译常量/枚举缺口

**验收**：`typecheck` 通过，所有渲染枚举/状态的地方改用 `t(key)` 显示

---

#### 步骤 3.2：Table columns / emptyText 批量化

**做什么**：
替换所有 Table 的列名和空状态文字。这是性价比最高的步骤——`title` 和 `emptyText` 替换简单且覆盖页面广。

**替换模式**：
```tsx
// 改造前
{ title: '空间名称', dataIndex: 'name' }
locale={{ emptyText: '暂无领域空间' }}

// 改造后
{ title: t('domainSpace.name'), dataIndex: 'name' }
locale={{ emptyText: t('domainSpace.empty') }}
```

**涉及文件**：
- `DomainSpace/index.tsx` — 列名 6+，emptyText 1，分页文案 1
- `DomainManagement/index.tsx` — emptyText 1
- `DomainKnowledgeCompileResults/RelationTab.tsx` — emptyText 1
- `DomainKnowledgeCompileResults/OntologyTab.tsx` — emptyText 1
- `DomainKnowledgeCompileResults/FeedbackList.tsx` — emptyText 1
- `DomainKnowledgeDetail/synonym/SynonymTab.tsx` — emptyText 1
- `DomainKnowledgeDetail/compile/CompileStepSection.tsx` — emptyText 1
- `DomainKnowledgeDetail/compile/TemplateImportModal.tsx` — emptyText 2
- `DomainKnowledgeDetail/compile/OntologyObjectSection.tsx` — emptyText 1
- `DomainKnowledgeDetail/compile/OntologyConstraintSection.tsx` — emptyText 1
- `DomainKnowledgeDetail/compile/OntologyRelationSection.tsx` — emptyText 1
- `DomainKnowledgeRelationList/index.tsx` — emptyText 1

**共约 18+ 处 emptyText，15+ 处列名**

**对应主计划**：§E.1 §3 — 表格列名、筛选器

---

#### 步骤 3.3：表单/弹窗/确认文案（按页面）

**做什么**：
按页面逐个替换 Form.Item label、placeholder、Modal title、确认文案。

**涉及文件及主要替换**：

**DomainSpace/index.tsx**（约 34 处）：
- 页面标题 `'领域空间管理'`
- 按钮 `'新建领域空间'`
- 搜索 `'搜索空间名称...'`
- 权限设置对话框全套文案：`'设置权限'`、`'保存权限'`、`'取消'`、`'添加成员'`、`'搜索用户名或邮箱...'`、`'未找到匹配用户'`、`'暂无可添加的用户'`、`'搜索已添加的成员...'`、`'未找到匹配的成员'`、`'暂无权限成员'`、`'查看'` / `'管理'` 角色标签、`'移除成员'` 标题
- 删除确认弹窗：`'确认删除'`、`'确定要删除领域空间"xxx"吗？'`、`'删除后该空间下的所有领域和知识库将被移除'`
- 分页 `'共 X 条'`、`'暂无领域空间，点击上方按钮新建'`

**SpaceForm/SpaceFormModal.tsx**（4 处）：
- `label="空间名称"`、`placeholder="例如：金融风控空间"`
- `label="空间描述"`、`placeholder="简要描述该空间的用途和范围"`

**DomainSpacePermission/index.tsx**（12 处）：
- 权限表格列名、角色标签、添加/移除操作文案

**DomainSpaceSettings/index.tsx**（9 处）：
- 设置表单 label、placeholder、保存成功提示

**对应主计划**：§E.1 §3 — 表单、弹窗、按钮、提示

---

#### 步骤 3.4：错误/加载/空状态

**做什么**：
替换各页面错误状态、加载提示和网络错误文案。

**涉及文件**：
- `api/request.ts:29` — `'请求失败'` → `'Request failed'` 或 `i18n.t('error.requestFailed')`
- `DomainSpace/index.tsx:298` — `'加载失败'` + `'重试'`
- `DomainSpace/index.tsx:62` — `'加载失败'`
- 其他各页面的 `message.error/info/success` 中的中文文案

**对应主计划**：§E.1 §4 — loading/empty/error/success 状态；§9.3 — 非组件模块使用 `i18n.t()`

---

#### 步骤 3.5：编译页面（复杂区域）

**做什么**：
编译相关页面是中文最密集、结构最复杂的区域，包含表单、弹窗、枚举下拉、步骤编排等多种 UI 元素。

**涉及文件**：
- `compile/ObjectFormModal.tsx` — 对象表单 label、placeholder、枚举选项
- `compile/RelationFormModal.tsx` — 关系表单 label、选项、验证文案
- `compile/OntologyRelationSection.tsx` — 关系列表列名、空状态、操作按钮
- `compile/CompileTab.tsx` — Tab 标题、操作按钮
- `compile/constants.ts` — 常量映射分离（已在步骤 3.1 处理）

**特殊注意事项**：
- 枚举下拉（`'一对一'`、`'一对多'` 等）：
  - 自定义选择器不要直接用 `t()` 返回 label 作为 value
  - 改用稳定 code（`'one_to_one'`），渲染时用 `t()` 显示
- 占位符和工具提示：
  - `placeholder` 使用 `t()`
  - `tooltip` 使用 `t()`

**对应主计划**：§E.1 §3 — 表单、弹窗；§E.4 — 业务值分离约束

---

#### 步骤 3.6：Mock 数据英文化

**做什么**：
将 `data/mock.ts` 和 `types/domainService.ts` 中的中文演示数据全部替换为英文。

**替换对照表样例**：

| 原始中文 | 替换为 |
|---|---|
| `'医疗健康知识空间'` | `'Healthcare Knowledge Space'` |
| `'金融科技知识空间'` | `'FinTech Knowledge Space'` |
| `'心血管疾病诊断专家共识2025版'` | `'Cardiovascular Disease Diagnosis Expert Consensus 2025'` |
| `'张明远'` | `'Alice Zhang'` |
| `'企业数字化转型'` (知识条目名) | `'Enterprise Digital Transformation'` |
| `'领域知识条目'` | `'Knowledge Items'` |
| 领域名称 `'医疗健康'` / `'金融科技'` | `'Healthcare'` / `'FinTech'` |

**验收标准**：
- mock 数据不含中文字符
- 英文默认界面加载英文 mock 数据
- 虚构数据不涉及真实姓名、邮箱、组织

**对应主计划**：§G.3 — 安全化示例与 mock；§E.1 §6 — mock/demo 数据

---

### Phase 4：验收

#### 步骤 4.1：自动门禁

```bash
cd frontends
pnpm run i18n:check        # 资源文件校验
pnpm --filter @jonex/core-business typecheck   # 类型检查
pnpm run lint               # 代码风格
pnpm --filter @jonex/core-business build       # 构建验证
```

**补充扫描**：
```bash
# 运行时硬编码中文扫描（排除 locales 目录和类型字面量）
grep -rn -I -E '[一-龥]' --include='*.ts' --include='*.tsx' core-business/src/ \
  | grep -v 'locales/' | grep -v node_modules | grep -v '\.test\.' | grep -v 'type\s*='

# Yuexi 品牌残留扫描
grep -rn -I -E 'Yuexi|yuexi|悦溪' -- include='*.ts' --include='*.tsx' core-business/src/ \
  | grep -v node_modules

# 敏感值扫描
grep -rn -I -E '(token|secret|password|api[_-]?key)' --include='*.ts' --include='*.tsx' core-business/src/ \
  | grep -v node_modules | grep -v '\.env\.'
```

**对应主计划**：§12.1 — 自动门禁；§F.3 — 品牌完成扫描

---

#### 步骤 4.2：双语言功能验收矩阵

| 场景 | 英文 | 中文 | hosted | standalone |
|---|---|---|---|---|
| 首页统计卡片和快捷入口 | 必测 | 必测 | 必测 | 必测 |
| 领域空间列表、新建/编辑/删除 | 必测 | 必测 | 必测 | 必测 |
| 权限设置对话框 | 必测 | 必测 | 必测 | 必测 |
| 知识库详情页各 Tab | 必测 | 必测 | 必测 | 必测 |
| 图谱页、源数据页、引擎页 | 必测 | 必测 | 必测 | 必测 |
| 编译页面表单和枚举下拉 | 必测 | 必测 | 必测 | 必测 |
| 搜索功能 placeholder 和结果 | 必测 | 必测 | 必测 | 必测 |
| Table 空状态、错误状态 | 必测 | 必测 | 必测 | 必测 |
| 运行中切换语言全量刷新 | 必测 | 必测 | 必测 | 必测 |
| 刷新后语言保持 | 必测 | 必测 | 必测 | 必测 |

**对应主计划**：§12.2 — 双语言功能矩阵；§15 — 执行检查表

---

## 4. 风险与注意事项

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 基础设施改造破坏现有 hosting | 子应用无法在 Shell 中加载 | 分步合并：先合并 i18n.ts 改造 → typecheck → 手动 hosted 验证 → 下一步 |
| `createI18nInstance()` 合并策略与预期不符 | 业务 key 未覆盖共享 key | 在独立沙盒中验证合并结果后再提交 |
| `fallbackLng` 从 `'zh'` 改为 `'en'` | 漏替换的 key 显示英文，中文用户看不到中文 | 最终必须通过 `i18n:check` + 人工扫描确保零缺 key |
| 删除 `HeaderNav/locale.tsx` 后 standalone 用户无法切换语言 | standalone 模式失去语言切换入口 | 确认 Shell 的 Dev Gateway 在 standalone 模式下提供语言切换能力 |
| Mock 数据改为英文后中文用户开发不便 | 本地开发时看到英文 mock | 这不是问题——目标用户是国际开源社区，本地开发应适配英文默认环境 |

---

## 5. 提交建议

参考主计划 §4，建议按以下原子提交流程：

```bash
# 从当前 dev 分支创建专用分支
git switch -c chore/core-business-i18n

# Phase 1：基础设施
git commit -m "feat(core-business): integrate shared i18n infrastructure"
# (i18n.ts + package.json + main.tsx + App.tsx + event listener + remove locale switcher + simplify locale files)

# Phase 2：业务资源
git commit -m "feat(core-business): add business translation keys"
# (zh.json + en.json 新增业务命名空间)

# Phase 3：运行时替换
git commit -m "i18n(core-business): separate enum values from display labels"
git commit -m "i18n(core-business): replace table columns and empty text"
git commit -m "i18n(core-business): replace form labels and modal text"
git commit -m "i18n(core-business): replace error and loading states"
git commit -m "i18n(core-business): internationalize compile pages"
git commit -m "i18n(core-business): anglicize mock data"

# Phase 4：验收
git commit -m "test(core-business): enforce i18n checks and verify dual-locale"
```

每个提交必须可 `typecheck`。

---

## 6. 工作量估算

| Phase | 步骤 | 预估修改文件数 | 预估工时 |
|---|---|---|---|
| P1 基础设施 | 1.1-1.6 | 8-10 | 中（2-3h） |
| P2 翻译资源 | 2.1-2.3 | 2 + ~250 keys | 中（3-4h） |
| P3 运行时替换 | 3.1 业务值分离 | 6 | 高（3-4h） |
| | 3.2 Table 批量 | 15+ | 中（1-2h） |
| | 3.3 表单/弹窗 | 5 | 中（2-3h） |
| | 3.4 错误/加载 | 8 | 低（1h） |
| | 3.5 编译页面 | 5 | 高（3-4h） |
| | 3.6 Mock 数据 | 2 | 低（1h） |
| P4 验收 | 4.1-4.2 | — | 中（2h） |

**总计**：约 18-24 小时
