# TCADP 插件 YAML 文件

本目录存放 TCADP 平台 API 插件的 OpenAPI YAML 定义文件。

## TCADP API 插件模式

1. **导出 YAML** - 将Jonex 平台的业务 API 导出为 OpenAPI 3.0 YAML
2. **导入 TCADP** - 在 TCADP 后台手动创建 API 插件并导入 YAML
3. **直接调用** - TCADP 直接调用平台业务路由

## 可用插件

> **注意**：TCADP 平台每个接口对应一个独立的 YAML 文件

当前暂无内置插件 YAML。

> **TCADP 限制**：仅支持 GET 和 POST 请求，不支持 PUT/DELETE

## 接入流程

### 1. 配置环境变量

```ini
# .env 文件中添加
TCADP_API_URL=https://tcadp.tencent.com/api
TCADP_API_KEY=your_tcadp_api_key
TCADP_WEBHOOK_URL=https://tcadp.tencent.com/webhook/callback
TCADP_WEBHOOK_SECRET=your_webhook_secret
```

### 2. 导入插件到 TCADP

1. 登录 TCADP 平台后台
2. 进入 "能力管理" → "API 插件"
3. 点击 "新建插件"
4. 选择 OpenAPI 导入方式
5. 上传对应的插件 YAML 文件
6. 配置插件认证方式（API Key / Signature）
7. 保存并启用插件

### 3. 验证调用

TCADP 会直接调用平台业务路由。

### 4. 签名验证（可选但推荐）

在业务路由中添加 TCADP 签名验证依赖：

```python
from api_gateway.routes.tcadp import verify_tcadp_signature

@router.post("/your-endpoint", dependencies=[Depends(verify_tcadp_signature)])
async def your_handler(...):
    ...
```

## 新增能力插件

1. 参考 TCADP 平台的插件规范创建新的 YAML 文件
2. 确保路径与 API Gateway 业务路由一致
3. 更新 `tcadp.py` 中的 `/v1/capabilities` 列表
