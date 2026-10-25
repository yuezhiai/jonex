import { normalizeTechnicalName } from './normalize.js';
import type { DomainDocument, EntityType } from './types.js';

/**
 * 追加合并前的「疑似同一概念但不同名」候选(供用户确认去重)。
 *
 * 判定只取**高精度**信号,不做名字模糊匹配:
 *  ① normalized displayName 全等;
 *  ② 别名交集 / 一方的别名命中另一方的 name/displayName(遵循 disambiguation.aliasMerge)。
 *
 * 实测校准(扫全库现有文档):displayName 全等命中全部真重复
 * (repair_tool↔tool、repair_process↔repair_procedure、maintenance_case↔repair_case、
 * repair_workflow↔repair_procedure)且零误报;而
 *  - name 段包含会把父子概念判重:`transformer` ⊂ `transformer_model/series/part/oil`
 *  - displayName 包含同样误报:`issuer` ⊂ `shareholder`、`distributor` ⊂ `electronic_component`
 * 误报会把父子/相关概念真的合并掉,比漏报危险得多,故两者都不采用。
 */
export interface DuplicateCandidate {
  incoming: { name: string; displayName?: string };
  current: { name: string; displayName?: string };
  reason: 'displayName' | 'alias';
}

/** 归一化:trim + 折叠内部空白后再走 name 归一化(遵循 caseInsensitive)。 */
const norm = (value: string, caseInsensitive: boolean): string =>
  normalizeTechnicalName(value.trim().replace(/\s+/g, ' '), caseInsensitive);

const brief = (entity: EntityType): { name: string; displayName?: string } =>
  entity.displayName !== undefined && entity.displayName !== '' ? { name: entity.name, displayName: entity.displayName } : { name: entity.name };

/**
 * 找出 incoming 中「未同名、但与 current 某个实体疑似同一概念」的实体对。
 * 同名对会被 mergeDocuments 自动合并,不算候选(无需用户确认)。
 */
export function findDuplicateCandidates(current: DomainDocument, incoming: DomainDocument): DuplicateCandidate[] {
  const { caseInsensitive: ci, aliasMerge } = current.model.disambiguation;
  const identity = (entity: EntityType): string[] =>
    [entity.name, entity.displayName ?? ''].map((value) => norm(value, ci)).filter((value) => value !== '');

  const currentKeys = new Set(current.model.entities.map((entity) => norm(entity.name, ci)));
  const aliasesOf = (entity: EntityType): string[] => entity.aliases.map((alias) => norm(alias, ci)).filter((alias) => alias !== '');

  const candidates: DuplicateCandidate[] = [];
  for (const ie of incoming.model.entities) {
    if (currentKeys.has(norm(ie.name, ci))) continue; // 同名 → 自动合并,无需确认
    for (const ce of current.model.entities) {
      // ① displayName 全等(两端都非空)
      const incomingLabel = ie.displayName !== undefined ? norm(ie.displayName, ci) : '';
      const currentLabel = ce.displayName !== undefined ? norm(ce.displayName, ci) : '';
      if (incomingLabel !== '' && incomingLabel === currentLabel) {
        candidates.push({ incoming: brief(ie), current: brief(ce), reason: 'displayName' });
        break;
      }
      // ② 别名交集 / 一方别名命中另一方标识
      if (aliasMerge) {
        const ia = aliasesOf(ie);
        const ca = aliasesOf(ce);
        const hit = ia.some((alias) => ca.includes(alias))
          || ia.some((alias) => identity(ce).includes(alias))
          || ca.some((alias) => identity(ie).includes(alias));
        if (hit) {
          candidates.push({ incoming: brief(ie), current: brief(ce), reason: 'alias' });
          break;
        }
      }
    }
  }
  return candidates;
}
