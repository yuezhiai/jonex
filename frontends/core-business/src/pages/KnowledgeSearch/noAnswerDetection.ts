/**
 * 检索结果的「无答案」判定。
 *
 * 只认**后端结构化信号**，不扫描答案正文。
 *
 * 历史实现用一串正则（`/无法提供/`、`/未找到相关/` …）去匹配整段答案，一旦命中就把
 * 后端返回的 references 整体清空。混合检索（`/search/mix`）的融合答案经常要解释
 * 「某个知识库未收录、无法提供该数据」，而引用其实来自同一批里的其他知识库——
 * 整段扫描会把真实引用一起清空，表现为同一个问题「有时有原文、有时没有」
 * （回答措辞每次不同，踩中正则才丢）。
 */

export interface NoAnswerMeta {
  source?: string | null;
  references?: unknown[] | null;
}

/**
 * 本次检索是否确实没有答案。
 *
 * 判据：
 * - 后端返回了任何引用 → 不是无答案（引用本身就是证据）；
 * - 否则只有当后端显式给出 `source === 'none'` 时才判为无答案。
 *
 * 后端在「所有管线都没有答案」时会置 `source = 'none'`，且该路径下 references
 * 本来就是空数组；这里的返回值只用于决定是否清空引用，因此不会误伤任何有引用的结果。
 */
export function isNoAnswerResponse(meta: NoAnswerMeta | null | undefined): boolean {
  const refs = Array.isArray(meta?.references) ? meta.references : [];
  if (refs.length > 0) return false;
  return meta?.source === 'none';
}
