# 悦溪平台前端 i18n 多语言改造文档

## 项目状态

| 项目 | 状态 | locale keys | 参考计划 |
|---|---|---|---|
| `shell` | ✅ 完成 | 122（含共享） | [i18n-migration-plan-shell.md](i18n-migration-plan-shell.md) |
| `core-business` | ✅ 完成 | 1,125 | [core-business-i18n-plan.md](core-business-i18n-plan.md) |
| `ecosystem-management` | ✅ 完成 | 413 | [i18n-migration-plan-ecosystem-management.md](i18n-migration-plan-ecosystem-management.md) |
| `platform-management` | ✅ 完成 | 408 | [i18n-migration-plan-platform-management.md](i18n-migration-plan-platform-management.md) |

## 核心文档

| 文档 | 说明 |
|---|---|
| [i18n-technical-summary.md](i18n-technical-summary.md) | **技术方案汇总** — 架构、数据流、配置模板、关键修复、新建子应用清单 |
| [i18n-architecture.md](i18n-architecture.md) | 架构设计详细文档 |
| [core-business-i18n-plan.md](core-business-i18n-plan.md) | core-business 改造原始计划 |

## 无需改造

- `shared/shell-sdk` — 无业务中文文本
- `shared/platform-theme` — 无业务中文文本
- `_template` — 仅少量示例中文
