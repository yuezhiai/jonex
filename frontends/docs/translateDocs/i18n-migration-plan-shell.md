# Shell 子应用 i18n 多语言改造计划

## 项目概况

- **目录**: `frontends/shell/`
- **工作量**: 44 行 / 9 个文件
- **当前 locale keys**: 0（使用共享 `@jonex/i18n-resources`，含 91 个通用 key）
- **i18n 基础设施**: 已就绪 — `src/locales/i18n.ts` 使用 `createI18nInstance()`
- **改造难度**: ⭐ 低（量小，且多数 UI 文本已有共享 key 可用）

## 共享 i18n-resources 可用 key

Shell 使用共享的 `@jonex/i18n-resources`，已有 91 个通用 key 可以直接复用：

**常用可直接复用的 key：**
| Key | 中文值 |
|---|---|
| `common.loading` | 加载中... |
| `common.retry` | 重试 |
| `common.back` | 返回 |
| `auth.login` | 登录 |
| `auth.loginSuccess` | 登录成功 |
| `auth.loginFailed` | 登录失败 |
| `auth.logout` | 退出登录 |
| `error.requestFailed` | 请求失败 |
| `error.networkError` | 网络异常，请稍后重试 |
| `error.unknownError` | 未知错误 |
| `status.disabled` | 禁用 |
| `space.add` | 添加领域空间 |
| `site.title` | Jonex Platform |

**需要新增的 key 命名空间建议：** `shell`、扩充 `common`

---

## 文件级改造清单

### 🔴 完全未开始（5 文件 / 14 行）

#### 1. `src/components/RemoteAppBoundary/index.tsx` — 5 行
| 行号 | 原文 | 建议 key | 说明 |
|---|---|---|---|
| L42 | `正在加载应用...` | `shell.loadingApp` 或 `common.loading` | Spin tip |
| L51 | `应用加载失败` | `shell.appLoadFailed` | Result title |
| L52 | `无法加载「{{appName}}」` | `shell.appLoadFailedDesc` | subTitle，含插值 |
| L55 | `重试` | `common.retry` | 按钮文字 |
| L59 | `在新窗口打开` | `shell.openInNewWindow` | 按钮文字 |

#### 2. `src/components/RemoteAppError/index.tsx` — 4 行
| 行号 | 原文 | 建议 key | 说明 |
|---|---|---|---|
| L41 | `应用运行出错` | `shell.appRuntimeError` | Result title |
| L42 | `「{{appName}}」运行时发生错误` | `shell.appRuntimeErrorDesc` | subTitle，含插值 |
| L45 | `重试` | `common.retry` | 按钮文字 |
| L47 | 打开 URL | `shell.openInWindow` | 按钮文字 |

#### 3. `src/pages/Dashboard/index.tsx` — 3 行
| 行号 | 原文 | 建议 key | 说明 |
|---|---|---|---|
| L32 | `{{name}}，欢迎使用悦溪平台` | `shell.welcome` | 欢迎语，含插值 |
| L40 | `智能知识管理与 AI 能力平台` | `shell.platformSubtitle` | 平台描述 |

#### 4. `src/navigation/navTypes.ts` — 1 行
| 行号 | 原文 | 说明 |
|---|---|---|
| L9 | `设计中` / `未来` | 类型字面量 `tag?: '设计中' \| '未来'`。**不需要翻译**（不是 UI 文本） |

#### 5. `src/api/spaces.ts` — 1 行
| 行号 | 原文 | 建议处理 |
|---|---|---|
| L22 | `throw new Error('获取空间列表失败')` | 改为英文 `throw new Error('Failed to fetch space list')` |

### 🟡 部分完成（4 文件 / 30 行）

#### 6. `src/pages/Login/index.tsx` — 18 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L70 | `请选择租户后继续登录` | `auth.selectTenantHint` |
| L76 | `欢迎，{{name}}` | `shell.loginWelcome` |
| L87 | `未知错误` | `error.unknownError`（已有） |
| L88 | `登录失败：` | `auth.loginFailed`（已有）+ 拼接 |
| L112 | `登录成功，但回跳地址不在允许范围内` | `auth.redirectNotAllowed` |
| L118 | `缺少回跳应用标识` | `auth.missingRedirectApp` |
| L126 | `登录态同步失败` | `auth.sessionSyncFailed` |

#### 7. `src/pages/AppHost/index.tsx` — 9 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L237 | `加载应用清单失败` | `shell.manifestLoadFailed` |
| L241 | `应用未找到` | `shell.appNotFound` |
| L245 | `应用已停用` | `shell.appDisabled` |
| L249 | `无访问权限` | `auth.noPermission`（已有） |
| L256 | `加载失败` | `common.loadFailed` |
| L257 | `无法加载「{{name}}」` | `shell.appLoadFailedDesc` |

#### 8. `src/components/SpaceSwitcher/index.tsx` — 2 行
| 行号 | 原文 | 建议 key |
|---|---|---|
| L149 | `添加领域空间` | `space.add`（已有） |

#### 9. `src/api/auth.ts` — 1 行
| 行号 | 原文 | 建议处理 |
|---|---|---|
| L107 | `throw new Error('请求失败')` | 改为英文 `throw new Error('Request failed')` |

---

## 改造步骤

### Phase 1：基础准备（~10 min）
1. 在 `shared/i18n-resources/src/locales/zh.json` 和 `en.json` 中新增 `shell` 命名空间的 key
2. 不需要修改 shell 的 `i18n.ts` — 已使用共享实例

### Phase 2：源码替换（~20 min）
1. 用 Agent 并行处理 3 组文件（或手动修改，量小）
2. 组 A：`RemoteAppBoundary` + `RemoteAppError`（9 行）
3. 组 B：`Login` + `AppHost` + `Dashboard`（30 行）
4. 组 C：`SpaceSwitcher` + `api/*`（4 行）

### Phase 3：验证（~10 min）
1. `pnpm run typecheck` — 编译验证
2. 扫描确认无残留中文
3. locale 文件同步检查（zh.json ↔ en.json key 一致）

---

## 预计新增 locale keys

| 命名空间 | key 数 | 示例 |
|---|---|---|
| `shell.*` | ~15 | `shell.loadingApp`, `shell.welcome` |
| `auth.*` | ~5 | `auth.selectTenantHint` |
| 扩充 `common.*` | ~2 | 按需 |

**合计新增约 20 个 key，总 key 数从 91 增至 ~111。**
