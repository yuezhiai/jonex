# @jonex/shared-lib

跨子应用（core-business / ecosystem-management 等）复用的公共组件与工具库。

## PermButton（权限按钮）

在 antd `Button` 全部参数基础上增加权限判断：无权限时禁用，悬停提示「无权限」。

| Prop | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `permissions` | `string[]` | 是 | 用户全部权限码（`userInfo.permissions`） |
| `requiredPerm` | `string \| string[]` | 是 | 按钮所需权限，数组任一命中即放行 |
| `noPermissionTip` | `boolean` | 否 | 无权限时是否 Tooltip 提示，默认 `true` |
| `noPermissionText` | `string` | 否 | 自定义提示文案，默认 `common.noPermission` |
| 其余 | `ButtonProps` | — | antd Button 参数原样透传 |

有权限时透传原生 Button；无权限时禁用并提示（显示禁用态而非隐藏，如需隐藏请调用侧自行过滤）。

```tsx
<PermButton
  type="primary"
  permissions={userPermissions}
  requiredPerm="mcp:key:manage"
  onClick={handleCreate}
>
  创建
</PermButton>
```

## usePermission（权限 hook）

从 shell 下发的 ShellContext（`window.__SHELL_CONTEXT__`）读取用户权限，hosted 与 standalone 模式均可用。

```ts
const { permissions, hasPerm } = usePermission();
hasPerm('mcp:key:manage');              // boolean
hasPerm(['mcp:key:view', 'mcp:key:manage']); // 任一满足即 true
```

- `permissions: string[]` —— 当前用户全部权限码
- `hasPerm(code: string | string[]): boolean` —— 判断是否拥有权限，数组任选其一

常与 `PermButton` 搭配：`<PermButton permissions={permissions} requiredPerm="mcp:key:manage" />`。

## 扩展约定

新组件/工具放入 `src/components`、`src/utils` 并在 `src/index.ts` 导出；i18n 通用文案放共享 `@jonex/i18n-resources` 的 `common`，业务文案放所属子应用；改源码后 `pnpm --filter @jonex/shared-lib build` 同步 dist。
