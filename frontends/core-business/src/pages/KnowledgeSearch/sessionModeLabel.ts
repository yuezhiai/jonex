/**
 * 检索会话卡片（SessionCard）的档位徽标 → i18n key。
 *
 * 早先这里用三元链只认 fast / deep，其余一律回落到 wikiSearch —— 「数据检索」与
 * 「本体 + 数据」都被错标成「Wiki 检索」。这里改成完整映射，新增档位时 TypeScript
 * 会因 `Record<KnowledgeSearchUiMode, string>` 缺键而报错，不会再静默回落。
 */
import type { KnowledgeSearchUiMode } from '@/types/knowledgeSearch';

export const SEARCH_MODE_LABEL_KEYS: Record<KnowledgeSearchUiMode, string> = {
  fast: 'knowledgeSearch.fastSearch',
  deep: 'knowledgeSearch.deepSearch',
  wiki: 'knowledgeSearch.wikiSearch',
  ontologyData: 'knowledgeSearch.ontologyDataSearch',
  data: 'knowledgeSearch.dataSearch',
};

/** 会话回放的历史数据 mode 可能缺失/未知 → 保持旧兜底（Wiki 检索）。 */
export function searchModeLabelKey(mode?: KnowledgeSearchUiMode): string {
  return (mode && SEARCH_MODE_LABEL_KEYS[mode]) || 'knowledgeSearch.wikiSearch';
}
