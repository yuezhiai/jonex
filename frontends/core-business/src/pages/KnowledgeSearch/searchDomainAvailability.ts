import type { KnowledgeSearchDomain } from '@/types/knowledgeSearch';

/**
 * 发起检索前的范围告警类型。
 *
 * - `none`：范围有效且有可检索的 KB，正常检索
 * - `noKb`：没选范围，或选中的范围确实没有 KB —— 沿用原「没有可查询的知识库」提示
 * - `unavailable`：选中的范围已不在候选中（停用 / 删除 / 跨空间）—— 需要用户重新选择
 */
export type DomainWarning = 'none' | 'noKb' | 'unavailable';

/**
 * 判定选中检索范围在**当前候选**里的可用性。
 *
 * 为什么要区分 `unavailable`：服务候选已由后端过滤掉停用服务（前端不再保留它们），
 * 但历史记录 / 已选状态里可能仍留着当时选中的服务 id。此时若只按
 * `kb_ids` 为空判成 `noKb`，会把「这个服务已经停用了」说成「这个领域没有知识库」，
 * 用户会反复去找一个不存在的 KB。找不到候选项就说明它已不可用。
 *
 * @param domainId 当前选中的检索范围 id（空 = 用户没选）
 * @param domains  当前候选范围列表（空间 + 后端过滤后的服务）
 */
export function resolveDomainWarning(
  domainId: string,
  domains: Pick<KnowledgeSearchDomain, 'id' | 'kb_ids'>[],
): DomainWarning {
  if (!domainId) return 'noKb';
  const picked = domains.find((d) => d.id === domainId);
  // 不在候选里：服务已停用 / 已删除 / 不属于当前空间
  if (!picked) return 'unavailable';
  return (picked.kb_ids?.length ?? 0) > 0 ? 'none' : 'noKb';
}
