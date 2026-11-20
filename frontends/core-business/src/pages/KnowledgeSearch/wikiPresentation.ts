/** Add layout breaks only when the answer already contains the complete, ordered section labels. */
export function formatWikiAnswer(content: string): string {
  if (!content || /```|~~~/.test(content)) return content;
  const markers = [...content.matchAll(/(^|\n|(?<=[。.!?])\s+)(Evidence|Interpretation|Conclusion|证据|解读|解释|结论)\s*[:：]/g)];
  const kinds = markers.map((m) => ['Evidence', '证据'].includes(m[2]) ? 'evidence'
    : ['Conclusion', '结论'].includes(m[2]) ? 'conclusion' : 'interpretation');
  if (markers.length !== 3 || kinds.join(',') !== 'evidence,interpretation,conclusion'
      || content.slice(0, markers[0].index).trim()) return content;
  let formatted = content;
  for (let i = markers.length - 1; i >= 0; i--) {
    const marker = markers[i];
    const at = marker.index!;
    const label = marker[0].slice(marker[1].length);
    formatted = formatted.slice(0, at) + `\n\n### ${label}\n\n` + formatted.slice(at + marker[0].length);
  }
  return formatted.trim();
}

export interface WikiTraceChoice {
  /** 流式 wiki 会话标记（后端 wiki 会话 id 存在即 true） */
  wikiSession?: unknown;
  /** 后端答题来源：jonex_wiki / mixed / ontology / rag … */
  source?: string | null;
  /** 引用里是否带 wiki 证据句柄（query_id） */
  hasWikiEvidence: boolean;
  /** 从 reasoning 明细里还原出的 wiki 引擎事件步数 */
  wikiTraceStepCount: number;
}

/**
 * 是否用 WikiTrace 渲染推理过程。
 *
 * 只有**真正的 wiki 检索**才走 WikiTrace：
 *   · 流式 wiki 会话 / 后端来源为 `jonex_wiki`；
 *   · 历史回放没有 source 的纯 wiki 会话：引用带 query_id **且**确实有落库的 wiki 事件。
 *
 * ⚠️ 不能只看「引用带 query_id」：`/search/mix` 的混合结果只要包含 wiki 知识库，
 * 引用就会带 query_id，但它不是 wiki 会话。误判会让 WikiTrace 去读并不存在的引擎
 * 事件，头部显示「0 步」并把后端返回的完整 `reasoning.steps` 整条吞掉。
 */
export function chooseWikiTraceView(choice: WikiTraceChoice): boolean {
  if (choice.wikiSession) return true;
  if (choice.source === 'jonex_wiki') return true;
  return (
    choice.source !== 'mixed'
    && choice.hasWikiEvidence
    && choice.wikiTraceStepCount > 0
  );
}
