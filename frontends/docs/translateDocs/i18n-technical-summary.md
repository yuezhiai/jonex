# 悦溪平台前端 i18n 多语言方案技术汇总

## 一、架构总览

```
┌─────────────────────────────────────────────────────────┐
│                        Shell (宿主)                      │
│  LocaleSwitcher ─→ i18n.changeLanguage() ─→ dispatchEvent │
│                    ↓                          ↓          │
│            localStorage(jonex_locale)  CustomEvent       │
│                                         (jonex:locale-change)│
└─────────────────────────────────────────────────────────┘
         ↕ CustomEvent                        ↕ CustomEvent
┌──────────────────────┐  ┌──────────────────────────────┐
│  core-business       │  │  ecosystem-management         │
│  (Module Federation) │  │  (Module Federation)          │
│  createI18nInstance()│  │  createI18nInstance()         │
│  GlobalLocaleListener│  │  GlobalLocaleListener         │
└──────────────────────┘  └──────────────────────────────┘
┌──────────────────────┐  ┌──────────────────────────────┐
│  platform-management │  │  shell (同进程)                │
│  createI18nInstance()│  │  createI18nInstance()         │
│  GlobalLocaleListener│  │  (不需要监听，自身是事件源)      │
└──────────────────────┘  └──────────────────────────────┘
```

### 核心机制

| 组件 | 职责 |
|---|---|
| `@jonex/i18n-resources` | 共享 locale 资源包，提供 `createI18nInstance()` 工厂函数 |
| `createI18nInstance()` | 创建 i18n 实例，自动 deep merge 共享 + 业务资源 |
| `I18nextProvider` | React 上下文提供者，将 i18n 实例传递给组件树 |
| `GlobalLocaleListener` | 监听 `jonex:locale-change` 事件，实时切换语言 |
| `AntdGate` / `AntdLocaleGate` | 读取当前语言，同步 Ant Design 组件 locale |
| `LocaleSwitcher`（Shell） | 用户语言切换 UI，dispatch CustomEvent 通知各子应用 |

### 数据流

```
用户点击语言切换
  → LocaleSwitcher.handleChange('en')
    → i18n.changeLanguage('en')           // 更新 Shell 自身
    → localStorage.setItem('jonex_locale', 'en')  // 持久化
    → dispatchEvent('jonex:locale-change')         // 通知子应用
      → 各子应用 GlobalLocaleListener 捕获事件
        → appI18n.changeLanguage('en')
          → useTranslation() 监听 languageChanged 事件
            → 所有组件重新渲染，t() 函数使用新语言
```

---

## 二、共享资源包 `@jonex/i18n-resources`

### 位置

`frontends/shared/i18n-resources/`

### 提供的能力

```typescript
// 核心导出
export function createI18nInstance(options?: {
  resources?: ResourceMap  // 业务方自定义资源
  lng?: string             // 初始语言（默认从 jonex_locale 读取）
}): I18nInstance
```

### 共享 locale keys

| 命名空间 | key 数 | 说明 |
|---|---|---|
| `common.*` | ~53 | 通用：保存/取消/删除/搜索/加载/名称/类型/操作 |
| `auth.*` | ~16 | 登录认证：用户名/密码/登录/登出/权限 |
| `error.*` | ~5 | 错误：404/403/500/网络异常/请求失败 |
| `navigation.*` | ~15 | 导航：首页/知识库/技能/提示词模板/系统设置 |
| `status.*` | ~11 | 状态：启用/停用/已完成/进行中/失败/草稿 |
| `site.*` | ~2 | 站点：平台名称/副标题 |
| `space.*` | ~2 | 空间：添加/选择领域空间 |
| `language.*` | ~3 | 语言：中文/English/切换语言 |
| `rules.*` | ~5 | 校验：必填/邮箱/手机号/长度 |
| `shell.*` | ~31 | Shell 特有：加载应用/登录流程/错误边界 |
| **合计** | **~143** | |

### deepMergeTranslations

```typescript
function deepMergeTranslations(
  base: TranslationRecord,     // 共享资源
  override: TranslationRecord  // 业务资源
): TranslationRecord {
  // 同名基础类型字段 → override 胜出
  // 同名对象字段 → 递归 merge
  // 业务方独有的字段 → 保留
}
```

---

## 三、各子应用 i18n 配置

### 标准配置模式（所有子应用统一）

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

### App.tsx（standalone 模式）

```tsx
import React, { useEffect, useState } from 'react'
import { I18nextProvider, useTranslation } from 'react-i18next'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import { antdTheme } from '@jonex/platform-theme'
import i18n from '@/locales/i18n'
import AppRoute from '@/router'

// 实时响应语言变化更新 Ant Design locale
function AntdGate({ children }: { children: React.ReactNode }) {
  const { i18n: appI18n } = useTranslation()
  const antdLocale = appI18n.language === 'zh' ? zhCN : enUS
  return <ConfigProvider locale={antdLocale} theme={antdTheme}>{children}</ConfigProvider>
}

// 监听 Shell 语言切换事件
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
    <I18nextProvider i18n={i18n}>
      <GlobalLocaleListener />
      <AntdGate>
        <AppRoute mode="standalone" />
      </AntdGate>
    </I18nextProvider>
  )
}
```

### RemoteApp.tsx（hosted 模式 / Module Federation）

```tsx
import React, { useEffect, useState } from 'react'
import { I18nextProvider, useTranslation } from 'react-i18next'
import { ConfigProvider } from 'antd'
import i18n from '@/locales/i18n'

// 同上模式 + shell locale 初始同步
export default function mount(container, shellContext) {
  const { locale: shellLocale } = shellContext || {}
  
  // 初始同步 shell locale
  if (shellLocale && shellLocale !== i18n.language) {
    i18n.changeLanguage(shellLocale)
  }

  root.render(
    <I18nextProvider i18n={i18n}>
      <LocaleController />
      <AntdLocaleGate>
        <AppRoute mode="hosted" />
      </AntdLocaleGate>
    </I18nextProvider>
  )
}
```

---

## 四、关键修复记录

### 🔴 问题 1：Storage Key 不一致

| 项目 | 写入 key | 读取 key |
|---|---|---|
| Shell | `jonex_locale` ✅ | `jonex_locale` ✅ |
| 旧 i18n.ts（子应用） | — | `locale` ❌ |
| 旧 store/global.ts | `locale` ❌ | `locale` ❌ |

**影响**：初始加载时子应用读不到 Shell 保存的语言值，默认显示中文。

**修复**：使用 `createI18nInstance()` 统一从 `jonex_locale` 读取。

### 🔴 问题 2：独立 i18n 实例导致缺少共享资源

**旧代码**：
```typescript
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
i18n.use(initReactI18next).init({
  lng: (getItem('locale') as string) || 'zh',
  resources: { zh: { translation: zhLocales }, en: { translation: enLocales } },
  // ❌ 没有加载 @jonex/i18n-resources 的共享 key
})
```

**影响**：菜单、导航等使用共享 key（`navigation.*`、`common.*`）的文本在英文模式下找不到翻译，fallback 到中文，看起来像是"语言没切换"。

**修复**：使用 `createI18nInstance()` 自动合并共享 + 业务资源。

### 🔴 问题 3：缺少 GlobalLocaleListener

子应用的 `App.tsx` 和 `RemoteApp.tsx` 没有监听 `jonex:locale-change` 事件，Shell 切换语言后子应用不刷新。

**修复**：所有子应用添加 `GlobalLocaleListener`（或 `LocaleController`）。

### 🔴 问题 4：Agent 换后 locale key 缺失

Workflow agent 修改源码使用了 `t('key')` 但未将 key 写入 locale 文件（约 30% 的 key 丢失）。

**影响**：运行时报 key not found，回退显示 key 名（如 `userManagement.title`）。

**修复**：需检查源码使用的 key 是否全部在 locale 文件中：
```bash
# 检查缺失 key 的脚本
python3 << 'EOF'
import re, os, json
with open('src/locales/zh.json') as f: zh = json.load(f)
def flat(d, p=''):
    r=set()
    for k,v in d.items():
        key=f'{p}.{k}' if p else k
        if isinstance(v,dict): r.update(flat(v,key))
        else: r.add(key)
    return r
existing = flat(zh)
used = set()
for root, dirs, files in os.walk('src'):
    dirs[:]=[d for d in dirs if d!='locales']
    for f in files:
        if not f.endswith(('.tsx','.ts')): continue
        with open(os.path.join(root,f)) as fh:
            for m in re.findall(r"t\('([a-zA-Z.]+)'\)", fh.read()):
                used.add(m)
missing = used - existing
print(f'缺失 {len(missing)} keys')
for k in sorted(missing): print(k)
EOF
```

---

## 五、各项目改造数据

| 指标 | shell | core-business | ecosystem-mgmt | platform-mgmt |
|---|---|---|---|---|
| **改造前 locale keys** | 91（共享） | 663 | 134 | 93 |
| **改造后 locale keys** | 122（含共享） | 1,125 | 413 | 408 |
| **新增 key** | 31 | 462 | 279 | 315 |
| **修改文件数** | 8 | 73 | 20 | 28 |
| **新增代码行** | 61 | 2,928 | 1,093 | 967 |
| **删除中文行** | 47 | 1,299 | 453 | 488 |
| **Agent 数** | 2 | 10 + 4 | 4 + 1 | 4 + 1 |
| **Agent tokens** | 91k | 981k+395k | 384k+383k | 337k |
| **TypeScript 编译** | ✅ | ✅ | ✅ | ✅ |
| **locale 同步** | ✅ | ✅ | ✅ | ✅ |
| **残留中文（非 UI）** | ~1 行 | ~105 行 | ~13 行 | ~24 行 |

---

## 六、新建子应用 i18n 接入清单

### 步骤 1：添加依赖

```json
// package.json
"dependencies": {
  "@jonex/i18n-resources": "workspace:*",
  "i18next": "^25.3.1",
  "react-i18next": "^15.6.0"
}
```

### 步骤 2：创建 i18n 实例

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

### 步骤 3：初始化 i18n

```typescript
// src/main.tsx — 添加 import
import './locales/i18n'
```

### 步骤 4：配置 App.tsx

添加 `I18nextProvider` + `GlobalLocaleListener` + `AntdGate`（参考 core-business 模板）

### 步骤 5：配置 RemoteApp.tsx（Module Federation）

添加 `I18nextProvider` + `LocaleController` + `AntdLocaleGate`（参考 core-business 模板）

### 步骤 6：创建 locale 文件

```bash
# 创建空的 locale 文件
echo '{}' > src/locales/zh.json
echo '{}' > src/locales/en.json
```

### 步骤 7：在代码中使用

```tsx
import { useTranslation } from 'react-i18next'

function MyComponent() {
  const { t } = useTranslation()
  
  return (
    <div>
      <h1>{t('myApp.pageTitle')}</h1>
      <Button>{t('common.save')}</Button>
    </div>
  )
}
```

---

## 七、注意事项

### 1. `createI18nInstance()` 是必须的

❌ 禁止直接用 `i18next.use(initReactI18next).init()` 创建实例，会导致：
- 缺少共享 key（common.*、navigation.* 等）
- storage key 不一致（读写 `locale` 而非 `jonex_locale`）

### 2. 所有子应用必须监听 `jonex:locale-change`

Shell 切换语言后通过 CustomEvent 通知子应用，不监听则无法实时切换。

### 3. Agent 批量替换后检查 key 完整性

Agent 可能有 20-30% 的遗漏率，替换后必须运行验证脚本检查源码 key 是否全部在 locale 文件中。

### 4. zh.json ↔ en.json 必须保持同步

每次修改 zh.json 都必须同步修改 en.json，否则英文模式下会 fallback 到中文，看起来像"语言没切换"。

### 5. defaultValue 推荐使用

```tsx
// 推荐 — 中文兜底，避免 key name 显示
t('myKey', { defaultValue: '原始中文' })
```
