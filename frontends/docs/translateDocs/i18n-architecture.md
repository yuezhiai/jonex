# 悦溪平台前端国际化技术架构

> 文档版本：v2.0  
> 最后更新：2026-07-17  
> 适用范围：Shell 主应用 + 全部子应用（core-business / platform-management / ecosystem-management）

---

## 目录

1. [总体架构](#1-总体架构)
2. [共享基础设施](#2-共享基础设施)
3. [Shell 主应用](#3-shell-主应用)
4. [子应用接入](#4-子应用接入)
5. [语言切换通信机制](#5-语言切换通信机制)
6. [翻译资源管理](#6-翻译资源管理)
7. [核心模块详解](#7-核心模块详解)
8. [技术决策记录](#8-技术决策记录)
9. [开发与调试指南](#9-开发与调试指南)
10. [故障排查](#10-故障排查)

---

## 1. 总体架构

### 1.1 架构分层

```text
┌─────────────────────────────────────────────────────────────────┐
│  @jonex/i18n-resources (共享基础设施层)                           │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  createI18nInstance() 工厂    LANGUAGE_STORAGE_KEY         │  │
│  │  SUPPORTED_LOCALES            normalizeLocale()            │  │
│  │  LANGUAGE_OPTIONS             共享 zh.json / en.json       │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                          ▲ 依赖注入
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│  Shell 主应用     │ │  core-business   │ │  platform/eco   │
│  (语言切换入口)    │ │  (业务子应用)     │ │  (业务子应用)     │
├─────────────────┤ ├─────────────────┤ ├─────────────────┤
│ I18nextProvider  │ │ I18nextProvider  │ │ I18nextProvider  │
│ LocaleSwitcher   │ │ LocaleController │ │ LocaleController │
│ 派发切换事件      │ │ 监听切换事件      │ │ 监听切换事件      │
│ 独立 i18n 实例   │ │ 独立 i18n 实例   │ │ 独立 i18n 实例   │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

### 1.2 核心原则

| 原则 | 说明 |
|---|---|
| **实例隔离** | 每个 Module Federation 应用使用独立的 i18next 实例，避免微前端沙箱冲突 |
| **事件驱动** | Shell 为唯一语言切换入口，子应用通过事件被动接收切换通知 |
| **资源分层** | 通用词条放在共享包，业务词条放在各子应用，深合并加载 |
| **默认英文** | 默认/fallback 语言为英文，locale 无效值时回退 `en` |
| **持久化** | 语言偏好通过 `jonex_locale` 键存储到 localStorage |

### 1.3 关键依赖

| 包 | 版本 | 用途 |
|---|---|---|
| `i18next` | ^25.3.1 | 国际化核心框架 |
| `react-i18next` | ^15.6.0 | React 绑定 |
| `@jonex/i18n-resources` | 0.1.0 (workspace) | 共享基础设施包 |

---

## 2. 共享基础设施

### 2.1 包结构

```text
frontends/shared/i18n-resources/
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts                    ← 导出工厂函数和常量
    └── locales/
        ├── zh.json                 ← 共享中文词典
        └── en.json                 ← 共享英文词典
```

### 2.2 导出 API

```typescript
// src/index.ts — 完整导出清单

/** 支持的语言列表 */
export const SUPPORTED_LOCALES = ['zh', 'en'] as const
export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number]

/** 语言切换 UI 选项 */
export const LANGUAGE_OPTIONS: { label: string; value: string }[] = [
  { label: '中文', value: 'zh' },
  { label: 'English', value: 'en' },
]

/** localStorage 存储键名 */
export const LANGUAGE_STORAGE_KEY = 'jonex_locale'

/** 规范化 locale 值，非法值回退到 'en' */
export function normalizeLocale(value?: string | null): SupportedLocale

/**
 * 创建独立 i18n 实例
 * @param options.resources - 业务翻译资源（按语言和 namespace 组织）
 * @param options.lng - 初始语言，缺省时从 localStorage 读取
 */
export function createI18nInstance(options?: {
  resources?: ResourceMap
  lng?: string
}): I18nInstance
```

### 2.3 createI18nInstance() 工厂详解

```typescript
export function createI18nInstance(options?: {
  resources?: ResourceMap
  lng?: string
}): I18nInstance {
  // 1. 创建全新实例（非 i18next 默认单例）
  const instance = i18n.createInstance()

  // 2. 加载共享翻译资源
  const resources: ResourceMap = {
    zh: { translation: { ...zhResources } },
    en: { translation: { ...enResources } },
  }

  // 3. 深合并业务资源（业务 key 覆盖共享同名校 space key）
  for (const lang of SUPPORTED_LOCALES) {
    const businessNamespaces = options?.resources?.[lang]
    if (!businessNamespaces) continue
    for (const [namespace, businessResources] of Object.entries(businessNamespaces)) {
      const commonResources = resources[lang][namespace] ?? {}
      resources[lang][namespace] = deepMergeTranslations(commonResources, businessResources)
    }
  }

  // 4. 初始化
  const initOptions: InitOptions = {
    fallbackLng: 'en',
    lng: normalizeLocale(options?.lng ?? localStorage.getItem(LANGUAGE_STORAGE_KEY)),
    interpolation: { escapeValue: false },
    resources,
  }

  void instance.use(initReactI18next).init(initOptions)
  return instance
}
```

#### 关键行为

- **`i18n.createInstance()` 而非 `i18n` 单例**：每个子应用拥有自己的实例，互不干扰
- **`deepMergeTranslations`**：递归合并共享和业务资源，业务层 key 优先级高于共享层
- **`fallbackLng: 'en'`**：当翻译 key 在当前语言中不存在时，回退到英文
- **`initReactI18next`**：将实例注册到 react-i18next 模块，使 `useTranslation()` 能获取到此实例

---

## 3. Shell 主应用

### 3.1 职责

Shell 是语言切换的**单一入口**，承担以下职责：

1. 初始化自己的 i18n 实例（仅有共享资源，无业务资源）
2. 提供 `LocaleSwitcher` 语言切换 UI
3. 切换时持久化语言偏好
4. 通过 **两种通信机制** 通知子应用（CustomEvent + postMessage）
5. 将语言偏好传递给新挂载的子应用

### 3.2 LocaleSwitcher 组件

```tsx
// shell/src/components/LocaleSwitcher/index.tsx
export default function LocaleSwitcher() {
  const { i18n } = useTranslation()

  const handleChange = (locale: string) => {
    if (locale === i18n.language) return

    // 1. 更新 Shell 自身的 i18n 实例
    i18n.changeLanguage(locale)

    // 2. 持久化到 localStorage
    localStorage.setItem(LANGUAGE_STORAGE_KEY, locale)

    // 3. 通知同 window 的子应用（Module Federation 托管模式）
    window.dispatchEvent(new CustomEvent('jonex:locale-change', { detail: locale }))

    // 4. 通知 iframe 中的子应用（standalone fallback 模式）
    document.querySelectorAll('iframe').forEach((iframe) => {
      iframe.contentWindow?.postMessage({ type: 'jonex:locale-change', locale }, '*')
    })
  }

  // 显示当前语言标签，下拉仅显示可切换的另一种语言
  const currentLabel = LANGUAGE_OPTIONS.find((o) => o.value === i18n.language)?.label
  const items = LANGUAGE_OPTIONS.filter((o) => o.value !== i18n.language)
    .map((opt) => ({ key: opt.value, label: opt.label }))

  return (
    <Dropdown menu={{ items, onClick: ({ key }) => handleChange(key) }}>
      <Button type="text">
        <GlobalOutlined /> {currentLabel} <CaretDownOutlined />
      </Button>
    </Dropdown>
  )
}
```

### 3.3 App 入口

```tsx
// shell/src/App.tsx（核心逻辑片段）
function App() {
  const { i18n } = useTranslation()

  // 初始化存储的 locale，首次访问默认英文
  const stored = localStorage.getItem(LANGUAGE_STORAGE_KEY)
  if (stored === null) {
    localStorage.setItem(LANGUAGE_STORAGE_KEY, i18n.language)
  }

  const antdLocale = i18n.language === 'zh' ? zhCN : enUS

  return (
    <ConfigProvider locale={antdLocale} theme={antdTheme}>
      <BrowserRouter>
        <Routes>{/* ... */}</Routes>
      </BrowserRouter>
    </ConfigProvider>
  )
}
```

### 3.4 ShellContext locale 传递

```tsx
// shell/src/pages/AppHost/index.tsx — 传递给子应用的 context
const shellContext = {
  // ...
  locale: localStorage.getItem('jonex_locale')
       || localStorage.getItem('locale')   // 兼容旧 key
       || 'en',
}
```

---

## 4. 子应用接入

### 4.1 子应用 i18n.ts

每个子应用有自己的 `src/locales/i18n.ts`，调用共享工厂并注入业务资源：

```typescript
// core-business/src/locales/i18n.ts
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

#### 资源合并策略

```text
createI18nInstance()
  ├── 加载共享资源 (zh.json / en.json)
  │     ├── common.save: "保存"
  │     ├── auth.logout: "退出登录"
  │     └── ...
  └── 深合并业务资源
        ├── common.uploadSuccess: "上传成功"     ← 新增（共享包无此 key）
        └── common.save: "保存"                  ← 覆盖（与共享包冲突时业务优先）
```

### 4.2 standalone 模式（独立运行）

```tsx
// core-business/src/App.tsx
import React, { useEffect, useState } from 'react'
import { I18nextProvider, useTranslation } from 'react-i18next'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import i18n from '@/locales/i18n'
import AppRoute from '@/router'

/** 响应式 Ant Design locale */
function AntdGate({ children }: { children: React.ReactNode }) {
  const { i18n: appI18n } = useTranslation()
  const antdLocale = appI18n.language === 'zh' ? zhCN : enUS
  return <ConfigProvider locale={antdLocale} theme={antdTheme}>{children}</ConfigProvider>
}

/** 全局语言事件监听（CustomEvent + postMessage） */
function GlobalLocaleListener() {
  const { i18n: appI18n } = useTranslation()
  const [, forceRender] = useState(0)

  useEffect(() => {
    const onCustomEvent = (e: CustomEvent<string>) => {
      appI18n.changeLanguage(e.detail)
      forceRender((n) => n + 1)
    }
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'jonex:locale-change') {
        appI18n.changeLanguage(e.data.locale)
        forceRender((n) => n + 1)
      }
    }
    window.addEventListener('jonex:locale-change', onCustomEvent as EventListener)
    window.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('jonex:locale-change', onCustomEvent as EventListener)
      window.removeEventListener('message', onMessage)
    }
  }, [])

  return null
}

export default function App() {
  return (
    <I18nextProvider i18n={i18n}>          ← 显式绑定额外的 i18n 实例
      <GlobalLocaleListener />              ← 监听语言切换事件
      <AntdGate>                             ← Ant Design 语言跟随
        <AppRoute mode="standalone" />
      </AntdGate>
    </I18nextProvider>
  )
}
```

### 4.3 hosted 模式（Module Federation 托管）

```tsx
// core-business/src/remote/RemoteApp.tsx
import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { I18nextProvider, useTranslation } from 'react-i18next'
import { ConfigProvider } from 'antd'
import i18n from '@/locales/i18n'
import AppRoute from '@/router'

/** 响应式 Ant Design locale */
function AntdLocaleGate({ children }: { children: React.ReactNode }) {
  const { i18n: appI18n } = useTranslation()
  const antdLocale = appI18n.language === 'en' ? enUS : zhCN
  return <ConfigProvider locale={antdLocale} theme={antdTheme}>{children}</ConfigProvider>
}

/** 语言切换事件监听 */
function LocaleController() {
  const { i18n: appI18n } = useTranslation()
  const [, forceRender] = useState(0)

  useEffect(() => {
    const handler = (e: CustomEvent<string>) => {
      appI18n.changeLanguage(e.detail)
      forceRender((n) => n + 1)
    }
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'jonex:locale-change') {
        appI18n.changeLanguage(e.data.locale)
        forceRender((n) => n + 1)
      }
    }
    window.addEventListener('jonex:locale-change', handler as EventListener)
    window.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('jonex:locale-change', handler as EventListener)
      window.removeEventListener('message', onMessage)
    }
  }, [])

  return null
}

function HostedApp({ shellContext }) {
  const { basePath, locale: shellLocale } = shellContext

  // 初始同步
  useEffect(() => {
    if (shellLocale && shellLocale !== i18n.language) {
      i18n.changeLanguage(shellLocale)
    }
  }, [])

  return (
    <I18nextProvider i18n={i18n}>
      <LocaleController />
      <AntdLocaleGate>
        <AppRoute basename={basePath} mode="hosted" shellContext={shellContext} />
      </AntdLocaleGate>
    </I18nextProvider>
  )
}

/** Module Federation 入口 */
export default function mount(container, shellContext) {
  ;(window as any).__SHELL_CONTEXT__ = shellContext || {}
  const root = createRoot(container)
  root.render(<HostedApp shellContext={shellContext || {}} />)
  return () => root.unmount()
}
```

### 4.4 main.tsx 防双重渲染

```typescript
// core-business/src/main.tsx
import './locales/i18n'  // ← 保证 i18n 实例先初始化

// Module Federation 远程模式下，Shell 会调用 mount 函数挂载子应用，
// 不走 main.tsx 的 standalone 初始化路径。
const isFederatedRemote = typeof window !== 'undefined' &&
  !!(window as any).__federation_shared__

const root = document.getElementById('root')
const shellContext = (window as any).__SHELL_CONTEXT__

if (shellContext && root) {
  mount(root, shellContext)
} else if (!isFederatedRemote) {
  // 非 Module Federation 远程模式时启动独立应用
  startStandalone()
}
```

---

## 5. 语言切换通信机制

### 5.1 通信通道对比

| 机制 | 适用场景 | 能否跨 iframe | 能否跨域 | 实现 |
|---|---|---|---|---|
| **CustomEvent** | Module Federation 同 window 模式 | ❌ | ❌ | `window.dispatchEvent` / `window.addEventListener` |
| **postMessage** | iframe standalone fallback 模式 | ✅ | ✅ (指定 origin) | `iframe.contentWindow.postMessage` / `window.addEventListener('message')` |
| **localStorage** | 页面刷新后语言保持 | ✅ (同源) | ❌ | `'jonex_locale'` key |

### 5.2 完整通信流程图

```text
Shell LocaleSwitcher
      │
      ├─ 1. i18n.changeLanguage(locale)        ← Shell 自身立即更新
      │
      ├─ 2. localStorage.setItem('jonex_locale') ← 持久化
      │
      ├─ 3. dispatchEvent('jonex:locale-change') ← 同 window 通知
      │     │
      │     └──→ core-business（MF 同 window）
      │           LocaleController.onCustomEvent()
      │             ├── appI18n.changeLanguage()
      │             └── forceRender()
      │
      └─ 4. postMessage({type:'jonex:locale-change'}) ← 跨 iframe 通知
            │
            └──→ core-business（iframe standalone）
                  GlobalLocaleListener.onMessage()
                    ├── appI18n.changeLanguage()
                    └── forceRender()
```

### 5.3 事件协议

```typescript
// CustomEvent 格式
new CustomEvent('jonex:locale-change', {
  detail: 'en' | 'zh',  // 目标语言代码
})

// postMessage 格式
{
  type: 'jonex:locale-change',  // 消息类型标识
  locale: 'en' | 'zh',          // 目标语言代码
}
```

### 5.4 响应链

当子应用收到切换事件时：

```
收到事件
  → appI18n.changeLanguage(newLocale)    ← 更新 i18next 实例语言
  → forceRender()                        ← 触发 React 重渲染
     → useTranslation() 组件重新渲染      ← 所有 t() 调用返回新语言文本
     → AntdGate / AntdLocaleGate          ← ConfigProvider locale 更新
       → Ant Design 组件语言跟随切换
```

---

## 6. 翻译资源管理

### 6.1 资源文件结构

#### 共享资源（`@jonex/i18n-resources`）

```json
{
  "common": {
    "save": "保存",
    "cancel": "取消",
    "delete": "删除",
    "loading": "加载中...",
    "noData": "暂无数据",
    "retry": "重试"
  },
  "auth": {
    "login": "登录",
    "logout": "退出登录",
    "username": "用户名",
    "password": "密码"
  },
  "error": {
    "networkError": "网络异常，请稍后重试",
    "notFound": "页面不存在",
    "serverError": "服务器错误"
  },
  "navigation": {
    "home": "首页",
    "coreBusiness": "核心功能",
    "domainKnowledge": "领域知识库",
    "platformManagement": "平台"
  },
  "rules": {
    "required": "请输入{{field}}",
    "maxLength": "最多输入{{max}}个字符"
  },
  "status": {
    "enabled": "启用",
    "disabled": "禁用",
    "active": "活跃",
    "inactive": "停用",
    "pending": "待处理",
    "processing": "处理中",
    "completed": "已完成",
    "failed": "失败"
  },
  "language": {
    "switch": "切换语言",
    "zh": "中文",
    "en": "English"
  },
  "site": {
    "title": "Jonex Platform",
    "subtitle": "AI 能力平台"
  },
  "space": {
    "select": "选择领域空间",
    "add": "添加领域空间"
  }
}
```

#### 业务资源（core-business）

```json
{
  "common": {
    "uploadSuccess": "上传成功",
    "deleteFailed": "删除失败",
    "viewAll": "查看全部",
    "expandSidebar": "展开侧边栏",
    "collapseSidebar": "收起侧边栏"
  },
  "domainSpace": {
    "management": "领域空间管理",
    "create": "新建领域空间",
    "name": "空间名称",
    "description": "描述",
    "createdAt": "创建时间",
    "empty": "暂无领域空间，点击上方按钮新建"
  },
  "compile": {
    "attrType": {
      "string": "字符串",
      "text": "文本",
      "number": "数值"
    },
    "cardinality": {
      "oneToOne": "一对一",
      "oneToMany": "一对多"
    }
  }
  // ... 更多业务命名空间
}
```

### 6.2 键命名规范

| 层级 | 命名空间 | 示例 |
|---|---|---|
| 共享 | `common.*` | `common.save`, `common.cancel` |
| 共享 | `auth.*` | `auth.login`, `auth.logout` |
| 共享 | `error.*` | `error.notFound` |
| 共享 | `navigation.*` | `navigation.domainKnowledge` |
| 共享 | `status.*` | `status.active`, `status.failed` |
| 业务 | `{module}.*` | `domainSpace.create`, `compile.attrType.string` |

### 6.3 资源加载策略

```text
请求翻译 key "common.save"
  1. 查子应用业务 zh.json → 有则返回
  2. 查共享包 zh.json → 有则返回
  3. 查子应用业务 en.json → 有则返回
  4. 查共享包 en.json → 有则返回
  5. 返回 key 本身作为 fallback
```

### 6.4 插值变量规范

```json
{
  "importSuccess": "Imported {{count}} items",
  "confirmDeleteMessage": "确定要删除 \"{{name}}\" 吗？",
  "total": "共 {{total}} 条，{{from}}-{{to}}"
}
```

- 插值变量使用双花括号 `{{variable}}`
- 中英文档中插值变量名称必须一致
- 禁止用字符串拼接构造不同语序的句子

---

## 7. 核心模块详解

### 7.1 i18n 实例生命周期

```text
应用启动
  │
  ├── import @/locales/i18n
  │     └── createI18nInstance()
  │           ├── i18n.createInstance()       ← 创建隔离实例
  │           ├── instance.use(initReactI18next) ← 注册到 react-i18next
  │           ├── deepMergeTranslations()     ← 合并共享+业务资源
  │           └── instance.init()             ← 初始化（异步）
  │
  ├── 用户操作切换语言
  │     └── i18n.changeLanguage(locale)
  │           ├── 更新内部 language 状态
  │           ├── 触发 languageChanged 事件     ← react-i18next 监听到此事件
  │           └── 重新加载对应语言的资源
  │
  └── 应用卸载
        └── 实例被 GC 回收（无 I18nextProvider 手动销毁）
```

### 7.2 I18nextProvider 与实例绑定

`I18nextProvider` 是 react-i18next 提供的 React Context provider：

```tsx
<I18nextProvider i18n={customInstance}>
  {/* 所有子组件中的 useTranslation() 都使用 customInstance */}
</I18nextProvider>
```

**为什么必须使用 I18nextProvider？**

在 Module Federation 微前端架构中，每个子应用独立打包，有自己的 `react-i18next` 模块副本。如果不使用 `I18nextProvider`，`useTranslation()` 将使用 `react-i18next` 模块内部注册的默认实例。由于各子应用有不同的 `react-i18next` 模块副本，默认实例可能不一致，导致语言切换不生效。

`I18nextProvider` 通过 React Context 显式传递 i18n 实例，绕过了模块级别的实例注册，确保：

1. 子应用内所有组件使用同一个 i18n 实例
2. 事件监听器中 `i18n.changeLanguage()` 和组件中 `useTranslation()` 操作的是同一个实例
3. 语言变化能正确触发 React 重渲染

### 7.3 Ant Design locale 同步

```tsx
function AntdGate({ children }) {
  const { i18n } = useTranslation()      // ← 在 I18nextProvider 内部
  const antdLocale = i18n.language === 'zh' ? zhCN : enUS
  return <ConfigProvider locale={antdLocale}>{children}</ConfigProvider>
}
```

工作原理：
1. `useTranslation()` 通过 `I18nextProvider` 获取 i18n 实例
2. `i18n.language` 响应语言变化→React 重渲染
3. `antdLocale` 重新计算→`ConfigProvider` 传入新 locale
4. Ant Design 所有组件使用新的 locale

### 7.4 业务值与显示标签分离

对于既是数据值又是显示标签的枚举，采用 `值 → translation key` 映射：

```typescript
// types/domainKnowledge.ts
export type OntologyAttrType = '字符串' | '数值' | '日期' | '枚举' | '文本' | '布尔'

// 值 → translation key 映射（供组件 t() 使用）
export const ATTR_TYPE_LABEL_KEYS: Record<string, string> = {
  '字符串': 'compile.attrType.string',
  '文本': 'compile.attrType.text',
  '数值': 'compile.attrType.number',
  // ...
}
```

```tsx
// 组件中使用
<Select options={ATTR_TYPE_OPTIONS.map(o => ({
  ...o,
  label: t(ATTR_TYPE_LABEL_KEYS[o.value] || o.value),
}))} />
```

---

## 8. 技术决策记录

### 8.1 为什么用 CustomEvent + postMessage 双重通信？

```
Module Federation 模式：同一 window → CustomEvent 即可
dev-gateway / iframe 模式：跨窗口 → 需要 postMessage
```

开发环境中，子应用通过 `fallback.mode: "standalone"` 以 iframe 方式加载。生产环境中使用 Module Federation（同 window）。两种模式都需要支持，因此使用双重通信。

### 8.2 为什么使用独立 i18n 实例而非单例？

| 方案 | 问题 |
|---|---|
| 全局单例 | Module Federation 各副本覆盖，无法隔离 |
| 独立实例 + I18nextProvider | 各应用隔离，通过 Context 显式传递 |

### 8.3 为什么 fallbackLng 是 'en'？

主计划要求（`frontend-i18n-brand-open-source-plan.md` §1）：默认语言为英文。确保开源项目国际用户的首次体验为英文。

### 8.4 为什么 localStorage key 是 'jonex_locale'？

主计划要求（§F1）：存储键使用 `jonex_*` 常量。兼容旧 key `'locale'` 通过 `||` 回退。

### 8.5 为什么 I18nextProvider 同时出现在 App.tsx 和 RemoteApp.tsx？

| 文件 | 适用模式 |
|---|---|
| `App.tsx` | standalone（独立浏览器访问） |
| `RemoteApp.tsx` | hosted（Module Federation 远程挂载） |

两者渲染路径不同，但都使用同一 `@/locales/i18n` 导出的 i18n 实例。

---

## 9. 开发与调试指南

### 9.1 添加新翻译 key

```bash
# 1. 通用 key → 共享包
frontends/shared/i18n-resources/src/locales/zh.json  +  en.json

# 2. 业务 key → 子应用
frontends/<app>/src/locales/zh.json  +  en.json
```

### 9.2 验证步骤

```bash
# 类型检查
pnpm --filter @jonex/<app> typecheck

# 语言文件一致性校验（脚本待移植）
# pnpm run i18n:check

# 构建验证
pnpm --filter @jonex/<app> build
```

### 9.3 运行时调试

在浏览器控制台验证语言切换机制：

```javascript
// 查看当前语言
localStorage.getItem('jonex_locale')

// 手动触发语言切换（模拟 Shell 通知）
window.dispatchEvent(new CustomEvent('jonex:locale-change', { detail: 'en' }))
window.dispatchEvent(new CustomEvent('jonex:locale-change', { detail: 'zh' }))

// 查看 i18n 资源是否加载
// React DevTools → 搜索组件 → 查看 useTranslation hook 状态
```

### 9.4 语言文件校验要点

两个语言文件必须保持：

- **key 集合一致**：不存在一个语言有 key 而另一个没有
- **嵌套结构一致**：不出现一个语言是字符串另一个是对象
- **插值变量一致**：`{{name}}` 在两种语言中都存在

---

## 10. 故障排查

### 10.1 常见问题

| 问题 | 原因 | 解决方案 |
|---|---|---|
| 切换语言后子应用无反应 | (1) iframe 模式 postMessage 未到达  (2) `I18nextProvider` 缺失 | 检查 `LocaleController` 中的 `message` 事件监听；确认 `I18nextProvider` 包裹了组件树 |
| 子应用显示翻译 key 字符串 | 翻译 key 在语言文件中不存在 | 检查 `zh.json` / `en.json` 是否包含该 key |
| 部分组件语言未切换 | 组件未在 `I18nextProvider` 内 | 检查组件树结构 |
| 刷新后语言重置为中文 | `localStorage` 读取了旧 key `'locale'` | 确认使用的是 `'jonex_locale'` 和新 `normalizeLocale()` |
| Ant Design 组件语言未切换 | `ConfigProvider locale` 未响应 `i18n.language` | 确认使用 `AntdGate` 组件模式 |
| 控制台 i18n 初始化警告 | `createI18nInstance` 在 StrictMode 下被执行两次 | 不影响功能，由 `void` 关键字安全丢弃 |

### 10.2 调试流程

```text
问题：语言切换不生效
  │
  ├─ 1. 检查 Shell LocaleSwitcher
  │     └─ 点击后 localStorage 是否更新 → 否 → 检查 handleChange
  │
  ├─ 2. 检查事件是否到达
  │     └─ 在子应用 console.log 看事件是否触发
  │         window.addEventListener('jonex:locale-change', console.log)
  │
  ├─ 3. 检查 i18n 实例
  │     └─ i18n.language 是否在 changeLanguage 后更新
  │
  ├─ 4. 检查 I18nextProvider
  │     └─ React DevTools → 检查组件树是否被 I18nextProvider 包裹
  │
  └─ 5. 检查 Ant Design
        └─ ConfigProvider locale 是否随 i18n.language 变化
```

### 10.3 控制台诊断命令

```javascript
// 一键诊断
(function() {
  console.group('🌐 i18n 诊断');
  console.log('localStorage.jonex_locale:', localStorage.getItem('jonex_locale'));
  console.log('localStorage.locale (old):', localStorage.getItem('locale'));

  // 检查 i18n 实例
  if (window.__i18n_diagnostic) {
    console.log('i18n.language:', window.__i18n_diagnostic.language);
    console.log('i18n.isInitialized:', window.__i18n_diagnostic.isInitialized);
  } else {
    console.warn('未找到 i18n 实例诊断对象');
  }
  console.groupEnd();
})();
```
