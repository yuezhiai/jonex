# core-business 国际化完成执行计划

> 参考主计划：`docs/open-source-migration-plans/frontend-i18n-brand-open-source-plan.md`
> 当前状态基线：`frontends/i18n-status.md`

---

## 现状总览

core-business 已完成的基础设施：

| 项目 | 状态 |
|---|---|
| `src/locales/i18n.ts` 使用 `createI18nInstance()` | ✅ |
| `zh.json` / `en.json` 业务命名空间 | ✅（`domainKnowledge.*`、`domainSpace.*`、`knowledgeSearch.*` 等） |
| `routes.config.ts` / `menu.config.ts` title 改 key | ✅ |
| `BasicLayout` / `HeaderNav` 使用 `t()` | ✅ |
| `RemoteApp.tsx` 监听 `jonex:locale-change` | ✅ |
| `I18nextProvider` / `ConfigProvider` locale 同步 | ✅ |

剩余硬编码中文分布（26 个文件，按数量降序）：

| 文件 | 行数 | 类别 |
|---|---|---|
| `src/data/mock.ts` | 58 | Mock demo 数据 |
| `src/pages/DomainSpace/index.tsx` | 34 | 页面级 UI 文案 |
| `src/pages/DomainKnowledgeDetail/compile/constants.ts` | 34 | 编译常量/枚举标签映射 |
| `src/api/domainKnowledge.ts` | 25 | 枚举映射表、状态文字 |
| `src/pages/DomainKnowledgeGraph/index.tsx` | 23 | 图谱页 UI 文案 |
| `src/pages/DomainKnowledgeSourceData/index.tsx` | 18 | 源数据页 UI 文案 |
| `src/pages/DomainSpacePermission/index.tsx` | 12 | 权限页面 UI 文案 |
| `src/types/domainKnowledge.ts` | 11 | 类型字面量（中文枚举值） |
| `src/pages/Home/index.tsx` | 9 | 首页 UI 文案 |
| `src/pages/DomainSpaceSettings/index.tsx` | 9 | 空间设置 UI 文案 |
| `src/pages/DomainKnowledgeParser/index.tsx` | 9 | 解析器配置 legacy 描述 |
| `src/types/domainService.ts` | 8 | 演示数据 |
| 其余 14 个文件 | 1-6 | 零散页面/组件文案 |

---

## 步骤 1：稳定业务值与显示标签分离

**对应主计划**：§9.4（E4 — 业务值与显示标签分离）、§2.3（编译常量/枚举缺口）

**做什么**：
将代码中的中文枚举值和状态文字与显示标签分离。保留底层稳定值不变（`'ready'`、`'one_to_one'`），在渲染处通过 translation key 映射显示文字。

**涉及文件**：

| 文件 | 改造内容 |
|---|---|
| `src/types/domainKnowledge.ts` | `ValidationSeverity = '高' \| '中' \| '低'` 保留类型不变，新增映射表 `severityLabelKeys: Record<ValidationSeverity, string>` |
| `src/types/domainKnowledge.ts` | `OntologyAttrType = '字符串' \| ...` — 同上处理 |
| `src/types/domainKnowledge.ts` | `OntologyRelationType = '一对一' \| ...` — 同上 |
| `src/types/domainKnowledge.ts` | 约束条件的旧中文值映射（`'互斥' → 'mutually_exclusive'`）— 改为 translation key 映射 |
| `src/api/domainKnowledge.ts` | `attrTypeLabel()` / `attrTypeReverse()` — 中文↔英文双向映射 → 改为 `attrTypeLabelKey()` 返回 translation key |
| `src/api/domainKnowledge.ts` | `cardinalityLabel()` / `cardinalityReverse()` — 同上 |
| `src/api/domainKnowledge.ts` | 状态文字映射 `ready → '入库·解析·编译'` → 新增 `statusLabelKey()` 返回 `status.ingested` 等 key |
| `src/api/dataSource.ts` | `statusText()` `'ready → '入库·解析·编译'` — 同上 |
| `src/components/datasource/DataSourceDocTable.tsx` | `includes('中')` → 改为判断英文 code `'parsing'` |
| `src/pages/DomainKnowledgeDetail/compile/constants.ts` | 34 行编译常量/枚举翻译映射 → 改为 translation key 映射 |

**验收标准**：
- 所有状态文字和枚举标签可通过 `t(key)` 获取
- 底层稳定值（API 枚举、status code）不变
- `typecheck` 通过

---

## 步骤 2：DomainSpace 页面国际化

**对应主计划**：§9.1（E1 替换顺序 §3 — 表格列名/筛选器/按钮/弹窗）、§9.2（React 组件 t()）

**做什么**：
替换 `src/pages/DomainSpace/index.tsx` 中 34 处中文文案为 `t()` 调用。

**替换清单**：

| 原始中文 | 替换为 | 建议 key |
|---|---|---|
| `'空间名称'` (Table 列) | `t('domainSpace.name')` | `domainSpace.name` |
| `'描述'` | `t('domainSpace.description')` | `domainSpace.description` |
| `'创建时间'` | `t('domainSpace.createdAt')` | `domainSpace.createdAt` |
| `'状态'` | `t('domainSpace.status')` | `domainSpace.status` |
| `'权限设置'` | `t('domainSpace.permissionSettings')` | `domainSpace.permissionSettings` |
| `'操作'` | `t('domainSpace.actions')` | `domainSpace.actions` |
| `'设置权限'` | `t('domainSpace.setPermission')` | `domainSpace.setPermission` |
| `'编辑'` | `t('common.edit')` | `common.edit` |
| `'删除'` | `t('common.delete')` | `common.delete` |
| `'领域空间管理'` (页面标题) | `t('domainSpace.management')` | `domainSpace.management` |
| `'搜索空间名称...'` | `t('domainSpace.searchPlaceholder')` | `domainSpace.searchPlaceholder` |
| `'新建领域空间'` | `t('domainSpace.create')` | `domainSpace.create` |
| `'加载失败'` | `t('common.loadFailed')` | `common.loadFailed` |
| `'重试'` | `t('common.retry')` | `common.retry` |
| `'暂无领域空间，点击上方按钮新建'` | `t('domainSpace.empty')` | `domainSpace.empty` |
| Modal `确认删除` / `取消` / 确认文本 | `t('common.confirmDelete')` 等 | `common.*` |
| 权限设置对话框全部文案 | `t('domainSpace.*')` | `domainSpace.*` |
| `'添加成员'` / `'搜索用户名或邮箱...'` / `'未找到匹配用户'` | `t('domainSpace.*')` | `domainSpace.*` |
| `'查看'` / `'管理'` 权限角色 | `t('permission.view')` / `t('permission.manage')` | `permission.*` |
| `共 ${t} 条，${range[0]}-${range[1]}` | 使用 Ant Design `showTotal` 内 `t()` | — |

**验收标准**：
- DomainSpace 列表页、权限对话框无硬编码中文
- 英文/中文切换后表格列名、弹窗、搜索 placeholder 全部刷新
- 空状态、错误状态双语显示正常

---

## 步骤 3：Home 首页国际化

**对应主计划**：§9.1（E1 替换顺序 §2 — 首页）

**做什么**：
替换 `src/pages/Home/index.tsx` 中 9 处中文。

**替换清单**：

| 原始中文 | 替换为 | 建议 key |
|---|---|---|
| `'知识条目'` (统计卡片) | `t('home.knowledgeItems')` | `home.knowledgeItems` |
| `'文档总数'` | `t('home.totalDocuments')` | `home.totalDocuments` |
| `'本月检索'` | `t('home.monthlySearches')` | `home.monthlySearches` |
| `'知识检索、领域空间、知识管理与领域配置'` | `t('home.welcomeDescription')` | `home.welcomeDescription` |
| `'文档'` (空间卡片) | `t('home.documents')` | `home.documents` |
| `'知识'` (空间卡片) | `t('home.knowledge')` | `home.knowledge` |
| `'领域配置'` 快捷入口 | `t('domainConfig.title')` | `domainConfig.title` |
| `'领域服务与检索'` 快捷入口描述 | `t('domainConfig.description')` | `domainConfig.description` |

**验收标准**：
- 首页统计卡片、快捷入口、空间卡片无中文
- 双语切换后首页全量刷新

---

## 步骤 4：图谱与源数据页面国际化

**对应主计划**：§9.1（E1 替换顺序 §3 — 表格/表单/弹窗）

**做什么**：
替换 `DomainKnowledgeGraph/index.tsx`（23 处）和 `DomainKnowledgeSourceData/index.tsx`（18 处）以及 `DomainKnowledgeEngine/index.tsx` 中的硬编码中文。

**涉及文件**：`src/pages/DomainKnowledgeGraph/index.tsx`、`src/pages/DomainKnowledgeSourceData/index.tsx`、`src/pages/DomainKnowledgeEngine/index.tsx`、`src/pages/DomainKnowledgeDetail/index.tsx`、`src/pages/DomainKnowledgeDataSource/index.tsx`

**典型替换模式**：
- Table 列名、toolbar 按钮、标签、empty state
- 面板标题、Tab 标题、面包屑
- 搜索 placeholder、筛选器标签
- 操作确认弹窗文案

**验收标准**：
- 三个页面的所有用户可见文案通过 `t()` 获取
- 操作反馈（`message.success/error/info`）使用 `t()` 或英文常量
- `typecheck` 通过

---

## 步骤 5：编译相关页面国际化

**对应主计划**：§9.4（E4）、§2.3（编译缺口）

**做什么**：
核心处理 `src/pages/DomainKnowledgeDetail/compile/` 目录下的编译页面组件。此区域包含大量中文标签、枚举映射、表单验证文案。

**涉及文件**：`ObjectFormModal.tsx`、`RelationFormModal.tsx`、`OntologyRelationSection.tsx`、`CompileTab.tsx`、`constants.ts`

**典型替换模式**：
- 表单 label（`'对象名称'`、`'属性列表'`、`'关系类型'` 等）
- 枚举下拉选项（`'一对一'`、`'一对多'` 渲染时用 `t()`）
- placeholder、tooltip、modal 确认文案
- `constants.ts` 中的中文映射 → 改为 key 映射

**验收标准**：
- 编译页面所有表单、弹窗、下拉选项无中文
- 枚举值在渲染时通过 `t()` 显示
- `typecheck` 通过，编译页功能正常

---

## 步骤 6：Page/Settings/Feature 组件国际化

**对应主计划**：§9.2、§9.3

**做什么**：
替换剩余的页面和功能组件中的硬编码中文。

**涉及文件及改造内容**：

| 文件 | 行数 | 改造内容 |
|---|---|---|
| `src/pages/DomainSpacePermission/index.tsx` | 12 | 权限表格列名、添加/移除操作、角色选项 `'管理员'`/`'编辑者'`/`'查看者'`、placeholder |
| `src/pages/DomainSpaceSettings/index.tsx` | 9 | 设置表单 label、placeholder、保存成功提示 |
| `src/features/SpaceForm/SpaceFormModal.tsx` | 4 | `label="空间名称"`、`placeholder`、`label="空间描述"` |
| `src/pages/DomainKnowledgeParser/index.tsx` | 9 | `legacy` 字段（`'大文档预处理'` 等）— 如果仅用于显示则改为 `t('parserConfig.*')`，对应 key 已存在则直接读 key |
| `src/pages/KnowledgeSearch/index.tsx` | 4 | 搜索页面中文文案 |
| `src/pages/DomainKnowledgeDatasourceManual/index.tsx` | 1 | `includes('中')` → 判断英文 code |
| `src/components/datasource/DataSourceDocTable.tsx` | 1 | `includes('中')` → 判断英文 code |

**验收标准**：
- 所有功能组件无直接中文文案
- 所有 `Form.Item label` 使用 `t()`
- 所有 `placeholder` 使用 `t()`

---

## 步骤 7：错误/工具模块国际化

**对应主计划**：§9.3（非组件模块）

**做什么**：
处理非组件模块中的中文文案。

**涉及文件**：

| 文件 | 改造内容 |
|---|---|
| `src/api/request.ts` | `'请求失败'` → 改为 `'Request failed'` 或 `i18n.t('error.requestFailed')` |
| `src/utils/utils.ts` | 1 处中文（如果存在）→ 改为 `t()` 或英文常量 |

**验收标准**：
- API 错误提示不出现中文硬编码
- 非组件模块使用 `import i18n from '@/locales/i18n'` + `i18n.t()` 确保语言切换后刷新

---

## 步骤 8：mock 数据英文化

**对应主计划**：§11.3（G3 — 安全化示例与 mock）、§9.1 §6

**做什么**：
替换 `src/data/mock.ts` 中 58 行中文演示数据为英文/虚构数据。

**替换清单**：

| 原始中文 | 替换为 |
|---|---|
| `'医疗健康知识空间'` | `'Healthcare Knowledge Space'` |
| `'金融科技知识空间'` | `'FinTech Knowledge Space'` |
| `'智能制造知识空间'` | `'Smart Manufacturing Knowledge Space'` |
| `'教育培训知识空间'` | `'Education & Training Knowledge Space'` |
| `'零售电商知识空间'` | `'Retail E-commerce Knowledge Space'` |
| `'能源环保知识空间'` | `'Energy & Environment Knowledge Space'` |
| 各空间描述 `'涵盖临床医学、药物研发...'` | 英文描述 |
| 知识条目标题 `'心血管疾病诊断专家共识2025版'` | `'Cardiovascular Disease Diagnosis Expert Consensus 2025'` |
| 各领域名称 `'医疗健康'`、`'金融科技'` 等 | 英文领域名 |
| 用户显示名 `'张明远'`、`'李芳'` 等 | `'Alice Zhang'`、`'Bob Li'` 等英文名 |
| 属性/关系 mock 数据 `'企业数字化转型'`、`'知识图谱'` 等 | 英文数据 |
| `src/types/domainService.ts` 中示例数据 `'金融产品知识库'` 等 | 英文名称 |

**验收标准**：
- mock 数据不包含中文
- 英文默认界面加载英文 mock 数据
- 虚构数据不涉及真实个人信息

---

## 步骤 9：语言文件补充与校验

**对应主计划**：§6.3（B3 自动校验）、§1（完成定义）

**做什么**：
补充 `src/locales/zh.json` 和 `src/locales/en.json` 中所有新增的 translation key，确保两种语言文件完全同步。

**预期新增 key 空间**：

| 命名空间 | 预估数量 | 用途 |
|---|---|---|
| `domainSpace.*` | ~15 | 空间管理页面 |
| `domainConfig.*` | ~5 | 领域配置页面 |
| `home.*` | ~10 | 首页统计和欢迎 |
| `permission.*` | ~8 | 权限角色标签 |
| `parserConfig.*` | ~10 | 解析器配置 |
| `compile.*` | ~15 | 编译页面（补充） |
| `error.*` | ~2 | 错误提示补充 |

**操作步骤**：
1. 完成步骤 1-8 后收集所有用到的 translation key
2. 写入 `zh.json`（中文翻译）和 `en.json`（英文翻译）
3. 运行 `cd frontends && pnpm run i18n:check` 验证：
   - JSON 合法且无重复键
   - 同一资源对的 key 集合一致
   - 节点类型一致（不出现一个语言是对象另一个是字符串）
   - 插值变量 `{{name}}` 集合一致
4. 修正校验失败项直至通过

**验收标准**：
- `i18n:check` 零错误
- `zh.json` 与 `en.json` 的 key 集合、嵌套结构、插值变量完全一致
- 两种语言的 fallback 逻辑正确（未填 key 回退英文展现）

---

## 步骤 10：最终验收

**对应主计划**：§12（测试与验收矩阵）、§15（执行检查表）

**做什么**：
全面验证国际化完成质量。

**门禁命令**：
```bash
cd frontends
pnpm run i18n:check
pnpm --filter @jonex/core-business typecheck
pnpm run lint
pnpm --filter @jonex/core-business build
```

**功能验收矩阵**（hosted + standalone 双模式）：

| 场景 | 英文 | 中文 |
|---|---|---|
| 首页统计卡片和快捷入口 | 必测 | 必测 |
| 领域空间列表、新建/编辑/删除 | 必测 | 必测 |
| 权限设置对话框 | 必测 | 必测 |
| 知识库详情页各 Tab | 必测 | 必测 |
| 图谱页、源数据页 | 必测 | 必测 |
| 编译页面表单和弹窗 | 必测 | 必测 |
| 搜索功能 placeholder 和结果 | 必测 | 必测 |
| 空状态、错误状态 | 必测 | 必测 |
| 运行中切换语言全量刷新 | 必测 | 必测 |
| 刷新后语言保持 | 必测 | 必测 |

**补充扫描**：
```bash
# 确认无新增运行时代码硬编码中文
grep -rn -I -E '[一-龥]' --include='*.ts' --include='*.tsx' core-business/src/ \
  | grep -v 'locales/' | grep -v node_modules | grep -v '\.test\.'
```

**验收标准**：
- 所有门禁命令通过
- 运行时硬编码中文扫描零结果（`locales/` 目录和类型定义中的中文值除外）
- 双语言功能矩阵全量通过
- hosted + standalone 模式均工作正常

---

## 执行顺序建议

```
步骤 1 ─── 枚举/状态分离（基础，被多页面引用）
    │
步骤 2 ─── DomainSpace（页面级，影响最大）
    │
步骤 3 ─── Home 首页
    │
步骤 4 ─── 图谱/源数据页面
    │
步骤 5 ─── 编译相关页面（复杂，涉及表单和枚举渲染）
    │
步骤 6 ─── 剩余 Page/Settings/Feature 组件
    │
步骤 7 ─── 错误/工具模块
    │
步骤 8 ─── mock 数据英文化（可并行）
    │
步骤 9 ─── 语言文件补充与 i18n:check（步骤 1-8 完成后统一做）
    │
步骤 10 ── 最终验收（typecheck + build + 双语冒烟）
```

> **并行建议**：步骤 2-8 可并行推进，每个步骤独立 `typecheck` 确保不破坏已有功能。步骤 1 必须最先完成，步骤 9-10 必须在所有替换完成后执行。
