/** 治理 UI 的等级 / 效果 / 判定标签与展示辅助（纯函数）。 */

export type GovernanceLevel = 'public' | 'internal' | 'confidential' | 'secret';

export const LEVEL_ORDER: GovernanceLevel[] = [
  'public', 'internal', 'confidential', 'secret',
];

export const LEVEL_TAGS: Record<string, { key: string; color: string }> = {
  public: { key: 'domainKnowledge.governance.levelPublic', color: 'default' },
  internal: { key: 'domainKnowledge.governance.levelInternal', color: 'blue' },
  confidential: { key: 'domainKnowledge.governance.levelConfidential', color: 'orange' },
  secret: { key: 'domainKnowledge.governance.levelSecret', color: 'red' },
};

export const EFFECT_TAGS: Record<string, { key: string; color: string }> = {
  allow: { key: 'domainKnowledge.governance.effectAllow', color: 'green' },
  deny: { key: 'domainKnowledge.governance.effectDeny', color: 'red' },
  mask: { key: 'domainKnowledge.governance.effectMask', color: 'orange' },
  row_filter: { key: 'domainKnowledge.governance.effectRowFilter', color: 'blue' },
};

export const DECISION_TAGS: Record<string, { key: string; color: string }> = {
  allow: { key: 'domainKnowledge.governance.decisionAllow', color: 'green' },
  deny: { key: 'domainKnowledge.governance.decisionDeny', color: 'red' },
  mask: { key: 'domainKnowledge.governance.decisionMask', color: 'orange' },
  row_filter: { key: 'domainKnowledge.governance.decisionRowFilter', color: 'blue' },
};

export function levelRank(level: string | null | undefined): number {
  const idx = LEVEL_ORDER.indexOf(String(level || '') as GovernanceLevel);
  return idx < 0 ? 0 : idx;
}

/** 未知值不崩：原样显示（后端新增档位时 UI 能看见）。 */
export function tagFor(meta: Record<string, { key: string; color: string }>,
                       value: string): { key: string; color: string } {
  return meta[value] ?? { key: value, color: 'default' };
}
