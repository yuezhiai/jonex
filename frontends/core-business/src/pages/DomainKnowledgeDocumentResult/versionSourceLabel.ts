import type { TFunction } from 'i18next';

/** 版本来源 → 展示文案。未知来源保留原值（历史/后端新枚举不吞成「上传」）。 */
export function versionSourceLabel(source: string, t: TFunction): string {
  switch (source) {
    case 'backfill':
      return t('domainKnowledge.versions.sourceBackfill');
    case 'rollback':
      return t('domainKnowledge.versions.sourceRollback');
    case 'merge':
      return t('domainKnowledge.versions.sourceMerge');
    case 'upload':
      return t('domainKnowledge.versions.sourceUpload');
    default:
      return source || t('domainKnowledge.versions.sourceUpload');
  }
}
