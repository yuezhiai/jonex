/** 禁止同查表对的状态 → 可执行动作矩阵（纯函数，组件与测试共用）。 */

export type ForbiddenStatus = 'proposed' | 'active' | 'disabled';
export type ForbiddenAction = 'adopt' | 'disable' | 'enable' | 'delete';

export const STATUS_TAGS: Record<string, { key: string; color: string }> = {
  proposed: { key: 'domainKnowledge.lineage.forbidden.statusProposed', color: 'gold' },
  active: { key: 'domainKnowledge.lineage.forbidden.statusActive', color: 'red' },
  disabled: { key: 'domainKnowledge.lineage.forbidden.statusDisabled', color: 'default' },
};

const ACTIONS: Record<string, ForbiddenAction[]> = {
  proposed: ['adopt', 'disable', 'delete'],
  active: ['disable', 'delete'],
  disabled: ['enable', 'delete'],
};

/** 未知状态（后端将来新增）返回空数组：不猜、也不崩。 */
export function actionsFor(status: string): ForbiddenAction[] {
  return ACTIONS[status] ?? [];
}
