/**
 * 落点行的纯函数（可单测）：源画布跳转路径与链接可见性。
 *
 * 「打开源画布」= 数据源级编译 schema 画布（`/domain-knowledge/{kbId}/schema/{dsId}`，
 * 与数据源页签「编辑 schema」同一路由；`docs/.../abstract-schema-complete-editable-design.md`
 * §五 落点面板）。
 */
import type { EntityLanding } from '../ontologyCanvas/domain/index.js';

/** 数据源级画布路由。路由定义见 `src/router/routes.config.ts` 的 `domain-knowledge/:kbId/schema/:dsId`。 */
export function sourceCanvasPath(kbId: string, dataSourceId: string): string {
  return `/domain-knowledge/${kbId}/schema/${dataSourceId}`;
}

/** 该落点能不能「打开源画布」。数据源已被删（source_gone）→ 画布无处可开：
 *  点进去只会回落到知识库级那份（画布页会挂「回落」标记），对用户是误导；
 *  其余失效（实体/属性在源里不在）照开 —— 画布本身要看的就是该源的现状。 */
export function canOpenSourceCanvas(landing: Pick<EntityLanding, 'staleReason'>): boolean {
  return landing.staleReason !== 'source_gone';
}
