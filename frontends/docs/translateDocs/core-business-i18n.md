# core-business 国际化改造文档

> 更新日期：2026-07-17
> 涉及文件：44 个文件变更（+1720 / -793 行）
> typecheck：✅ 通过

---

## 一、架构概览

```text
@jonex/i18n-resources (共享翻译资源包)
  ├── src/locales/zh.json          ← 共享中文词典
  ├── src/locales/en.json          ← 共享英文词典
  └── src/index.ts                 ← createI18nInstance() 工厂函数

Shell (@jonex/shell)
  ├── src/locales/i18n.ts          ← 调用 createI18nInstance()，无业务资源
  ├── LocaleSwitcher               ← 语言切换器（地球图标 + 当前语言名）
  └── 派发 jonex:locale-change     ← CustomEvent + postMessage

core-business (@jonex/core-business)
  ├── src/locales/i18n.ts          ← 调用 createI18nInstance() + 注入业务资源
  ├── src/locales/zh.json          ← 业务中文词典
  ├── src/locales/en.json          ← 业务英文词典
  ├── src/App.tsx                  ← I18nextProvider 包装 standalone 模式
  └── src/remote/RemoteApp.tsx     ← I18nextProvider + 事件监听（hosted 模式）
```

---

## 二、关键文件清单

### 2.1 新增文件

| 文件 | 说明 |
|---|---|
| `src/locales/i18n.ts` | `createI18nInstance()` 工厂初始化 i18n |
| `src/locales/zh.json` | 中文业务翻译资源 |
| `src/locales/en.json` | 英文业务翻译资源 |

### 2.2 修改文件

#### 基础设施层

| 文件 | 变更 |
|---|---|
| `package.json` | 添加 `@jonex/i18n-resources: "workspace:*"` 依赖 |
| `src/locales/i18n.ts` | 从独立初始化 → `createI18nInstance()` 共享工厂 |
| `src/App.tsx` | 添加 `I18nextProvider` 包装，`GlobalLocaleListener` 监听语言事件 |
| `src/main.tsx` | 检测 Module Federation 模式跳过 `startStandalone()` |
| `src/remote/RemoteApp.tsx` | hosted 模式使用 `I18nextProvider` + `LocaleController` + `AntdLocaleGate` |

#### 状态与工具层

| 文件 | 变更 |
|---|---|
| `store/global.ts` | 删除 `locale` 属性和 `setLocale` 方法 |
| `types/domainKnowledge.ts` | 新增 `severityLabelKey`、`ontologyStatusLabelKey`、`compileScopeLabelKey`、`compileTriggerLabelKey`、`constraintTargetTypeLabelKey` |
| `types/domainService.ts` | Mock 数据英文化 |
| `api/request.ts` | 中文错误提示 → 英文 `'Request failed'` |

#### UI 组件层

| 文件 | 替换内容 |
|---|---|
| `components/HeaderNav/index.tsx` | `i18next.t()` → `t()`，`global.locale` → `i18n.language`，删除 locale 切换器导入 |
| 🗑️ `components/HeaderNav/locale.tsx` | **已删除**（语言切换由 Shell 统一提供） |
| `components/datasource/DataSourceDocTable.tsx` | emptyText 使用 `t()` |

#### 页面层

| 文件 | 替换数 |
|---|---|
| `pages/DomainSpace/index.tsx` | **34 处** — 列名、按钮、弹窗、权限、错误提示 |
| `pages/Home/index.tsx` | **15 处** — 统计卡片、快捷入口、状态标签 |
| `pages/DomainKnowledgeDetail/compile/CompileStepSection.tsx` | 列名、按钮、emptyText |
| `pages/DomainKnowledgeDetail/compile/OntologyObjectSection.tsx` | 列名、emptyText |
| `pages/DomainKnowledgeDetail/compile/OntologyRelationSection.tsx` | 列名、状态标签、emptyText |
| `pages/DomainKnowledgeDetail/compile/OntologyConstraintSection.tsx` | 列名、emptyText |
| `pages/DomainKnowledgeDetail/compile/TemplateImportModal.tsx` | 列名、emptyText |
| `pages/DomainKnowledgeDetail/compile/CompileTab.tsx` | 状态标签、删除确认 |
| `pages/DomainKnowledgeDetail/compile/ObjectFormModal.tsx` | 表单 label、弹窗 |
| `pages/DomainKnowledgeDetail/compile/RelationFormModal.tsx` | 表单 label、弹窗、关系类型选项 |
| `pages/DomainKnowledgeDetail/compile/ConstraintFormModal.tsx` | 表单 label、弹窗 |
| `pages/DomainKnowledgeBlank/index.tsx` | 统计标签、按钮、数据源标签 |
| `pages/DomainKnowledgeBlank/DocumentLibrary/index.tsx` | 文件夹 CRUD 消息、删除确认 |
| `pages/DomainKnowledgeBlank/DocumentLibrary/config.tsx` | 列名、状态标签、操作菜单 |
| `pages/DomainKnowledgeBlank/DocumentLibrary/UploadModal.tsx` | 上传反馈消息 |
| `pages/DomainKnowledgeBlank/DocumentLibrary/TagModal.tsx` | 标签弹窗文案 |
| `features/SpaceForm/SpaceFormModal.tsx` | 表单 label、placeholder、弹窗 |
| `pages/DomainManagement/index.tsx` | emptyText |
| `pages/DomainKnowledgeParser/index.tsx` | 预处理选择器 placeholder |
| `pages/DomainKnowledgeGraph/index.tsx` | 页面文案 |
| `pages/DomainKnowledgeSourceData/index.tsx` | 页面文案 |
| `pages/DomainKnowledgeCompileResults/RelationTab.tsx` | emptyText |
| `pages/DomainKnowledgeCompileResults/OntologyTab.tsx` | emptyText |
| `pages/DomainKnowledgeCompileResults/FeedbackList.tsx` | emptyText |
| `pages/DomainKnowledgeDetail/synonym/SynonymTab.tsx` | emptyText |
| `pages/DomainKnowledgeRelationList/index.tsx` | emptyText |
| `pages/KnowledgeSearch/index.tsx` | 页面文案 |
| `pages/DomainSpacePermission/index.tsx` | 页面文案 |
| `pages/DomainSpaceSettings/index.tsx` | 页面文案 |
| `data/mock.ts` | Mock 数据全英文化 |

---

## 三、架构设计

### 3.1 语言切换流程

```
Shell LocaleSwitcher
  │ 点击切换语言
  ├─→ i18n.changeLanguage(locale)         ← 更新 Shell 自身
  ├─→ localStorage.setItem('jonex_locale') ← 持久化
  ├─→ dispatchEvent('jonex:locale-change') ← 通知同 window 子应用
  └─→ postMessage({type:'jonex:locale-change'}) ← 通知 iframe 子应用
        │
        ▼
core-business LocaleController / GlobalLocaleListener
  ├─→ addEventListener('jonex:locale-change')  ← CustomEvent
  ├─→ addEventListener('message')              ← postMessage
  │     └─→ appI18n.changeLanguage(locale)     ← 更新子应用 i18n
  │     └─→ forceRender()                      ← 触发 React 重渲染
  │
  ▼ 所有组件
  ├─→ useTranslation() 重新渲染              ← i18n 文案更新
  └─→ ConfigProvider locale 更新             ← Ant Design 组件语言更新
```

### 3.2 hosted 模式（Module Federation）

```tsx
// RemoteApp.tsx
function HostedApp({ shellContext }) {
  return (
    <I18nextProvider i18n={i18n}>
      <LocaleController />       ← 监听语言事件
      <AntdLocaleGate>           ← 响应式 Ant Design locale
        <AppRoute ... />
      </AntdLocaleGate>
    </I18nextProvider>
  )
}
```

### 3.3 standalone 模式（独立运行）

```tsx
// App.tsx
export default function App() {
  return (
    <I18nextProvider i18n={i18n}>
      <GlobalLocaleListener />   ← 监听语言事件（含 iframe postMessage）
      <AntdGate>
        <AppRoute ... />
      </AntdGate>
    </I18nextProvider>
  )
}
```

### 3.4 i18n 实例初始

```typescript
// src/locales/i18n.ts
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

---

## 四、翻译资源结构

### 4.1 共享资源（`@jonex/i18n-resources`）

```text
shared/i18n-resources/src/locales/
├── zh.json          ← 通用中文翻译
│   ├── common.*       ← 增删改查、确认取消等
│   ├── auth.*         ← 登录、登出、权限
│   ├── error.*        ← 网络错误、页面未找到等
│   ├── navigation.*   ← 导航菜单
│   ├── rules.*        ← 表单验证规则
│   ├── status.*       ← 启用/禁用、活跃/停用等
│   ├── language.*     ← 语言名称
│   ├── site.*         ← 站点信息
│   └── space.*        ← 领域空间选择
└── en.json           ← 通用英文翻译（对应结构）
```

### 4.2 业务资源（core-business）

```text
src/locales/
├── zh.json           ← 业务中文翻译
│   ├── common.*         ← 业务扩展：uploadSuccess、deleteFailed 等
│   ├── auth.*           ← 登录页特有文案
│   ├── reset.*          ← 密码重置
│   ├── rules.*          ← 业务表单验证
│   ├── error.*          ← 业务错误页
│   ├── permission.*     ← 权限角色（查看/管理/管理员）
│   ├── domainSpace.*    ← 领域空间管理
│   ├── home.*           ← 首页
│   ├── domainConfig.*   ← 领域配置
│   ├── knowledgeSearch.* ← 知识检索
│   ├── parserConfig.*   ← 解析器配置
│   ├── validation.*     ← 验证级别
│   ├── compile.*        ← 编译引擎（attrType/cardinality/scope/trigger/step 等）
│   ├── status.*         ← 状态扩展（imported/parsing/compiling 等）
│   └── dataSource.*     ← 数据源
└── en.json           ← 业务英文翻译（对应结构）
```

---

## 五、翻译 key 命名规范

| 前缀 | 用途 | 示例 |
|---|---|---|
| `common.*` | 通用 CRUD、全局提示 | `common.saveSuccess`, `common.loadFailed` |
| `auth.*` | 认证相关 | `auth.signIn`, `auth.logout` |
| `error.*` | 错误页面和消息 | `error.404`, `error.network` |
| `navigation.*` | 导航菜单 | `navigation.domainKnowledge` |
| `rules.*` | 表单验证 | `rules.required`, `rules.email` |
| `status.*` | 状态标签 | `status.active`, `status.parsing` |
| `permission.*` | 权限角色 | `permission.view`, `permission.manage` |
| `{module}.*` | 模块级业务翻译 | `domainSpace.name`, `compile.attrType.string` |
| `validation.*` | 验证级别 | `validation.severity.high` |

---

## 六、使用规范

### 6.1 React 组件

```tsx
import { useTranslation } from 'react-i18next'

function MyComponent() {
  const { t } = useTranslation()
  return <Button>{t('common.save')}</Button>
}
```

### 6.2 带插值的文案

```json
// en.json
{ "importSuccess": "Imported {{count}} items" }
```

```tsx
message.success(t('synonym.importSuccess', { count: 10 }))
```

### 6.3 业务值与显示标签分离

```typescript
// ❌ 错误：用中文做 data value
const type = '字符串'

// ✅ 正确：用稳定 code 做 data value，用 t() 做显示标签
const type = 'string'
const display = t('compile.attrType.string')
```

### 6.4 Table 列名

```tsx
// ❌ 错误：硬编码中文
{ title: '空间名称', dataIndex: 'name' }

// ✅ 正确：使用 t()
{ title: t('domainSpace.name'), dataIndex: 'name' }
```

### 6.5 emptyText

```tsx
// ❌ 错误
locale={{ emptyText: '暂无数据' }}

// ✅ 正确
locale={{ emptyText: t('common.noData') }}
```

### 6.6 message 反馈

```tsx
// ❌ 错误
message.success('保存成功')

// ✅ 正确
message.success(t('common.saveSuccess'))
```

### 6.7 非组件模块

```typescript
// 通用 API 层抛出错误 code，由 UI 映射文案
// 需要时导入 i18n 实例
import i18n from '@/locales/i18n'

new Error(i18n.t('common.loadFailed'))
```

---

## 七、验收标准

### 7.1 自动门禁

```bash
cd frontends
pnpm --filter @jonex/core-business typecheck
pnpm --filter @jonex/core-business build
```

### 7.2 双语功能矩阵

| 场景 | 英文 | 中文 | hosted | standalone |
|---|---|---|---|---|
| 首页统计卡片和快捷入口 | 必测 | 必测 | 必测 | 必测 |
| 领域空间列表、新建/编辑/删除 | 必测 | 必测 | 必测 | 必测 |
| 权限设置对话框 | 必测 | 必测 | 必测 | 必测 |
| 知识库详情页各 Tab | 必测 | 必测 | 必测 | 必测 |
| 图谱页、源数据页、引擎页 | 必测 | 必测 | 必测 | 必测 |
| 编译页面表单和枚举下拉 | 必测 | 必测 | 必测 | 必测 |
| Table 空状态、错误状态 | 必测 | 必测 | 必测 | 必测 |
| 运行中切换语言全量刷新 | 必测 | 必测 | 必测 | 必测 |
| 刷新后语言保持 | 必测 | 必测 | 必测 | 必测 |

### 7.3 补充扫描

```bash
# 运行时硬编码中文扫描（排除 locales 目录和类型字面量）
grep -rn -I -E '[一-龥]' --include='*.ts' --include='*.tsx' core-business/src/ \
  | grep -v 'locales/' | grep -v node_modules | grep -v '\.test\.' | grep -v 'type\s*='
```

---

## 八、已知遗留项

| 项目 | 文件 | 建议 |
|---|---|---|
| ~10 处 `message.error('中文')` | `AddDataSourceModal`、`DocumentViewer`、`KnowledgeGraphPanel` 等 | 改为 `t()` + 补充对应翻译 key |
| 预处理选项是数据值 | `DomainKnowledgeParser` 中 `preprocessOptions` | 前端和后端双用的稳定值，保留中文 |
| 类型字面量 | `types/domainKnowledge.ts` 中 `ValidationSeverity`、`OntologyAttrType` | 保留为稳定值，新增 `*LabelKey` 映射供 `t()` 使用 |
| `i18n:check` 脚本 | 未从 `@jonex` 分支移植 | 需从开源分支移植校验脚本 |
| E2E 测试 | 测试中含中文定位文本 | 需按英文/中文分别跑测试 |
