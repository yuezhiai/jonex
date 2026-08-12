# MCP Key 管理 — 前端开发文档

> 面向生态管理子应用前端开发者。后端 API 已全部就绪，本文档涵盖联调所需的全部信息。

---

## 一、页面定位

| 项 | 说明 |
|---|---|
| 子应用 | `@jonex/ecosystem-management` |
| 开发端口 | `5176` |
| 生产路径 | `http://localhost/apps/ecosystem-management/mcp-keys` |
| 菜单归属 | 生态管理 → 侧边栏，「适配器管理」同级新增「MCP Key 管理」 |

---

## 二、后端 API 参考

所有 API 路径前缀：`/api/v1/platform/mcp-keys`

认证方式：Bearer Token（由 `apiClient` 拦截器自动附加，无需手动处理）。

响应信封统一格式：

```json
{
  "success": true,
  "code": 0,
  "message": "操作成功",
  "data": { ... }
}
```

### 2.1 创建 MCP Key

```
POST /api/v1/platform/mcp-keys
```

**Request Body：**

```json
{
  "name": "我的 MCP Key",          // string, 可选, max 255, 默认 ""
  "permissions": ["read"],         // string[], 可选, 仅允许 "read" 和 "*", 默认 ["read"]
  "allowed_kb_ids": []             // string[], 可选, 最多 100 个元素, 每个最长 64 字符, 默认 []
}
```

**Response `data`：**

```json
{
  "id": "a1b2c3d4e5f6...",        // 32 字符 hex (UUID4)，后续操作需要用此 ID
  "plaintext": "yxm_xxxxxxxx...",  // 明文 Key，仅此一次返回！
  "name": "我的 MCP Key",
  "key_prefix": "xxxxxxxx",        // yxm_ 后的 8 位前缀
  "permissions": ["read"],
  "allowed_kb_ids": [],
  "created_at": "2026-08-03T10:00:00.000Z"
}
```

> ⚠️ `plaintext` 仅在创建响应中返回一次。关闭弹窗/刷新页面后无法再次获取，必须引导用户立即复制。

### 2.2 获取 MCP Key 列表

```
GET /api/v1/platform/mcp-keys
```

无需分页参数，返回当前租户下**全部** Key（含已撤销）。

**Response `data`：**

```json
{
  "items": [
    {
      "id": "a1b2c3d4e5f6...",
      "name": "我的 MCP Key",
      "key_prefix": "xxxxxxxx",
      "permissions": ["read"],
      "allowed_kb_ids": [],
      "created_by": "admin",
      "created_at": "2026-08-03T10:00:00.000Z",
      "revoked_at": null,
      "last_used_at": null
    }
  ],
  "total": 1
}
```

> 注意：列表中**不含** `plaintext` 和 `key_hash`。`revoked_at` 有值 = 已撤销。

### 2.3 获取单个 MCP Key 详情

```
GET /api/v1/platform/mcp-keys/{key_id}
```

`key_id` 为 32 字符 hex 字符串。返回结构与列表项相同。

### 2.4 撤销 MCP Key

```
POST /api/v1/platform/mcp-keys/{key_id}/revoke
```

无 Request Body。成功响应 `message: "MCP Key 已撤销"`。**幂等操作**——对已撤销的 key 再次调用不报错。

### 2.5 重置 MCP Key

```
POST /api/v1/platform/mcp-keys/{key_id}/reset
```

**Request Body**（可选，不传则继承旧 Key 配置）：

```json
{
  "name": "新名称",               // string | null, 可选
  "permissions": ["*"],            // string[] | null, 可选
  "allowed_kb_ids": ["kb-001"]     // string[] | null, 可选
}
```

**Response `data`：** 与创建接口返回结构完全相同（含新的 `id` 和 `plaintext`）。

> 行为：旧 Key 立即撤销 + 新 Key 生成。重置后的新 Key 有全新的 `id`，旧 `id` 失效。

---

## 三、权限枚举

| 值 | 含义 |
|---|---|
| `"read"` | 只读权限（默认） |
| `"*"` | 全部权限 |

创建和重置时，permissions 字段只能包含这两个值。

---

## 四、TypeScript 类型定义

建议在 `frontends/ecosystem-management/src/api/mcpKeys.ts` 中定义：

```typescript
// ===== 请求类型 =====

export interface McpKeyCreatePayload {
  name?: string;
  permissions?: ('read' | '*')[];
  allowed_kb_ids?: string[];
}

export interface McpKeyResetPayload {
  name?: string | null;
  permissions?: ('read' | '*')[] | null;
  allowed_kb_ids?: string[] | null;
}

// ===== 响应类型 =====

export interface McpKeyItem {
  id: string;
  name: string;
  key_prefix: string;
  permissions: ('read' | '*')[];
  allowed_kb_ids: string[];
  created_by: string | null;
  created_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface McpKeyCreateResult {
  id: string;
  plaintext: string;       // ⚠️ 唯一可见机会
  name: string;
  key_prefix: string;
  permissions: string[];
  allowed_kb_ids: string[];
  created_at: string;
}

export interface McpKeyListResponse {
  items: McpKeyItem[];
  total: number;
}
```

---

## 五、API Service 层代码

新增文件 `frontends/ecosystem-management/src/api/mcpKeys.ts`：

```typescript
import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

function unwrap<T>(payload: ApiEnvelope<T>): T {
  if (!payload?.success) {
    throw new Error(payload?.message || 'Request failed');
  }
  return payload.data as T;
}

/** 全量列出当前租户下所有 MCP Key */
export async function listMcpKeys(): Promise<McpKeyListResponse> {
  const resp = await apiClient.get<ApiEnvelope<McpKeyListResponse>>('/api/v1/platform/mcp-keys');
  return unwrap(resp.data);
}

/** 创建 MCP Key，返回值含一次性 plaintext */
export async function createMcpKey(data: McpKeyCreatePayload): Promise<McpKeyCreateResult> {
  const resp = await apiClient.post<ApiEnvelope<McpKeyCreateResult>>('/api/v1/platform/mcp-keys', data);
  return unwrap(resp.data);
}

/** 撤销 MCP Key（幂等） */
export async function revokeMcpKey(keyId: string): Promise<void> {
  await apiClient.post(`/api/v1/platform/mcp-keys/${keyId}/revoke`);
}

/** 重置 MCP Key，返回新明文 */
export async function resetMcpKey(keyId: string, data?: McpKeyResetPayload): Promise<McpKeyCreateResult> {
  const resp = await apiClient.post<ApiEnvelope<McpKeyCreateResult>>(
    `/api/v1/platform/mcp-keys/${keyId}/reset`,
    data ?? {},
  );
  return unwrap(resp.data);
}
```

### 关于 apiClient

`apiClient` 是 axios 实例，已在 `src/api/client.ts` 中配置：
- **baseURL**: `/`
- **自动附加 Authorization header**（从 `@jonex/shell-sdk` 的 `readAccessToken()` 获取）
- **自动附加 X-Lang header**（`zh-CN` / `en-US`）
- **401 自动处理**（清除认证状态 + 触发 token 过期事件）

调用后端 API 时路径直接写 `/api/v1/...` 即可，不需要加域名或端口前缀。

---

## 六、路由与菜单配置

### 6.1 添加路由

编辑 `frontends/ecosystem-management/src/router/routes.config.ts`：

```typescript
// 在文件顶部 import 区新增
const McpKeyManagement = loadableComponent(() => import('@/pages/McpKeyManagement'));

// 在 getRoutes() 的 children 数组中新增路由（与其他路由同级）：
{ path: 'mcp-keys', element: McpKeyManagement, title: 'navigation.mcpKeyManagement' },
```

### 6.2 添加菜单

编辑 `frontends/ecosystem-management/src/router/menu.config.ts`：

```typescript
// 在 IconMap 中新增（可选 KeyOutlined 或其他图标）
import { KeyOutlined } from '@ant-design/icons';

export const IconMap: Record<string, ComponentType> = {
  // ...现有映射
  KeyOutlined,
};

// 在 menuConfig 数组中新增菜单项（位置按需调整）：
{
  key: 'mcp-keys',
  path: '/mcp-keys',
  icon: 'KeyOutlined',
  label: 'navigation.mcpKeyManagement',
  roles: ['admin', 'user'],
},
```

如果 MCP Key 管理应该是适配器管理下的子页面而非独立菜单项，则在 AdapterManagement 页面中使用 Tabs 组件切换子视图，不需要修改 menu.config.ts。

---

## 七、页面组件实现指南

### 7.1 建议的页面结构

```
src/pages/McpKeyManagement/
├── index.tsx                # 列表页主组件
├── CreateKeyModal.tsx       # 创建弹窗 + 明文展示
├── RevokeConfirmModal.tsx   # 撤销确认弹窗
├── ResetKeyModal.tsx        # 重置弹窗 + 新明文展示
└── index.css                # 样式
```

### 7.2 列表页

参考现有模式 `src/pages/TemplateDomains/index.tsx`（卡片列表 + 弹窗）或 `src/pages/../UserManagement/index.tsx`（表格列表）。

**必须覆盖的状态：**

| 状态 | 处理方式 |
|---|---|
| **Loading** | `<Spin size="large" />` 居中展示 |
| **Error** | `<Result status="error">` + 重试按钮 |
| **Empty** | `<Empty description="...">` + 创建引导 |
| **List** | 表格或卡片展示正常数据 |
| **Refresh** | 重试/刷新按钮调用 `load()` |

列表展示字段：

| 列 | 数据来源 |
|---|---|
| 名称 | `item.name`，为空时显示 `-` |
| key_prefix | `item.key_prefix`（`yxm_` 后的 8 位），方便用户识别 |
| 权限 | `item.permissions.join(', ')` 映射为 Tag（read=蓝色, *=金色） |
| KB 范围 | `item.allowed_kb_ids` 空数组 → 「全部知识库」Tag；有值 → 显示前 3 个 + "+N" |
| 创建时间 | `item.created_at`，格式 `YYYY-MM-DD HH:mm` |
| 最后使用 | `item.last_used_at`，为 null 时显示「从未使用」 |
| 状态 | `item.revoked_at` 有值 → Tag danger「已撤销」，无值 → Tag success「有效」 |
| 操作 | 撤销按钮 / 重置按钮 |

**操作按钮逻辑：**
- 「撤销」按钮：仅当 `revoked_at === null` 时显示
- 「重置」按钮：始终显示（已撤销的 key 也可重置，生成新 key）

### 7.3 创建弹窗

参考 `src/pages/TemplateDomains/DomainFormModal.tsx` 的 `forwardRef` + `useImperativeHandle` 模式。

**表单字段：**

| 字段 | 组件 | 校验 |
|---|---|---|
| 名称 | `<Input>` | 可选，自动 trim |
| 权限 | `<Select mode="multiple">` | 选项: `read`、`*`，默认 `['read']` |
| KB 范围 | `<Select mode="tags">` | 可选，输入 KB ID 回车添加 |

**创建成功后展示明文 Key：**
- 关闭表单 → 弹出新的 `<Modal>` 或替换表单内容展示明文
- 明文 key 使用 `<Input.Password>` + `<Typography.Text copyable>` 组合
- 明确提示：「Key 仅显示一次，请立即复制保存」
- 用户确认已复制后关闭弹窗 → 刷新列表

### 7.4 撤销确认弹窗

参考 `src/pages/TemplateDomains/DomainDeleteModal.tsx` 的模式。

- 展示被撤销 Key 的 name + key_prefix
- 确认文案：「撤销后该 Key 将立即失效，所有使用该 Key 的客户端将无法连接」
- `okButtonProps={{ danger: true }}`
- 成功后 `message.success` + 刷新列表

### 7.5 重置弹窗

可选设计两种方式：
1. **简单重置**：确认弹窗 → 直接调用 reset API（不传 body，继承旧配置）→ 展示新明文
2. **带编辑重置**：与创建表单类似，允许修改 name/permissions/kb_ids → 调用 reset API → 展示新明文

推荐**方式 1**（简单重置），与撤销弹窗模式相同，成功后展示新 Key。

---

## 八、i18n 翻译

在 `frontends/ecosystem-management/src/locales/zh.json` 中新增（`en.json` 同步添加）：

```json
{
  "mcpKeyManagement": {
    "pageTitle": "MCP Key 管理",
    "pageSubtitle": "管理 MCP Server 的访问密钥",
    "createBtn": "创建 Key",
    "createTitle": "创建 MCP Key",
    "revokeTitle": "撤销 MCP Key",
    "revokeConfirm": "撤销后该 Key 将立即失效，所有使用该 Key 的客户端将无法连接。",
    "revokeSuccess": "MCP Key 已撤销",
    "resetTitle": "重置 MCP Key",
    "resetConfirm": "重置将立即撤销旧 Key 并生成新 Key，请确保更新所有客户端配置。",
    "resetSuccess": "MCP Key 已重置",
    "nameLabel": "名称",
    "namePlaceholder": "输入 Key 名称（可选）",
    "permissionLabel": "权限",
    "permissionRead": "只读",
    "permissionAll": "全部",
    "kbRangeLabel": "知识库范围",
    "kbRangeHint": "留空授权所有知识库",
    "keyPrefix": "Key 前缀",
    "permissions": "权限",
    "allowedKb": "KB 范围",
    "allKb": "全部知识库",
    "createdBy": "创建者",
    "createdAt": "创建时间",
    "lastUsed": "最后使用",
    "neverUsed": "从未使用",
    "status": "状态",
    "activeStatus": "有效",
    "revokedStatus": "已撤销",
    "plaintextTitle": "Key 已生成",
    "plaintextWarning": "此 Key 仅显示一次，请立即复制并妥善保存。关闭后无法再次查看。",
    "copyBtn": "复制",
    "copied": "已复制",
    "searchPlaceholder": "搜索 Key 名称或前缀",
    "emptyText": "暂无 MCP Key",
    "createFirst": "创建第一个 Key",
    "loadFailed": "加载 MCP Key 列表失败"
  }
}
```

在 `navigation` 中新增菜单文本：

```json
{
  "navigation": {
    "mcpKeyManagement": "MCP Key 管理"
  }
}
```

现有 `common` 命名空间已有 `confirm`、`cancel`、`save`、`delete`、`refresh`、`retry` 等通用键，可以直接复用，无需新增。

---

## 九、现有代码模式参考

### 9.1 API 调用模式

文件：`src/api/adapters.ts`、`skills.ts`

核心模式：
```typescript
import apiClient from './client';

// 统一解包
function unwrapEnvelope<T>(payload: ApiEnvelope<T>): T {
  if (!payload?.success) throw new Error(payload?.message || 'Request failed');
  return payload.data as T;
}

// GET
const resp = await apiClient.get<ApiEnvelope<XxxResponse>>('/api/v1/...');
return unwrapEnvelope(resp.data);

// POST
const resp = await apiClient.post<ApiEnvelope<XxxResponse>>('/api/v1/...', body);
return unwrapEnvelope(resp.data);
```

### 9.2 弹窗 Modal 模式

文件：`src/pages/TemplateDomains/DomainFormModal.tsx`

核心模式：
- `forwardRef` + `useImperativeHandle` 暴露 `open()` 方法
- 父组件通过 `ref.current.open(data?)` 打开弹窗
- 表单使用 `<Form form={form}>` + `form.validateFields()`
- 保存按钮 `confirmLoading={saving}` 防止重复提交
- 成功后 `message.success()` + `onSuccess?.()` 回调刷新列表

### 9.3 列表页模式

文件：`src/pages/TemplateDomains/index.tsx`

核心模式：
- `useState` 管理 `items`、`loading`、`error`、搜索/筛选状态
- `useCallback` + `useEffect` 加载数据
- 五种状态分支渲染：loading → error → empty → 列表 → 分页
- 弹窗组件通过 `ref` 驱动

---

## 十、开发命令

```bash
# 进入前端目录
cd frontends

# 安装依赖（首次）
pnpm install

# 启动生态管理子应用
pnpm --filter @jonex/ecosystem-management dev
# 访问 http://localhost:5176

# 类型检查
pnpm --filter @jonex/ecosystem-management typecheck

# 构建
pnpm --filter @jonex/ecosystem-management build

# 完整前端启动（通过开发网关）
pnpm run dev:gateway
# 访问 http://localhost:8080/apps/ecosystem-management
```

---

## 十一、后端代码参考位置

| 层 | 文件 |
|---|---|
| API 路由 | `capabilities/platform/api/mcp_key_api.py` |
| 服务层 | `capabilities/platform/services/mcp_key_service.py` |
| DTO | `capabilities/platform/dtos/mcp_key_dto.py` |
| 仓储 | `capabilities/platform/repository/mcp_key_repository.py` |
| ORM 模型 | `capabilities/platform/models/mcp_key.py` |

---

> 如有疑问可通过此文档提交人转达后端侧。
