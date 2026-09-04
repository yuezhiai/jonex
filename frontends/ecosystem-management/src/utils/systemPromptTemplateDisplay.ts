import type { PromptTemplateItem, VersionItem } from '../api/promptTemplates';

type Translate = (key: string) => string;

const SYSTEM_TEMPLATE_KEYS: Record<string, string> = {
  seed_pt_sys_docsum: 'docSummary',
  seed_pt_sys_email: 'emailReply',
  seed_pt_sys_qa: 'knowledgeQa',
};

const DOMAIN_TEMPLATE_KEYS: Record<string, string> = {
  seed_pt_dom_contract: 'contractReview',
  seed_pt_dom_report: 'reportAnalysis',
};

function templateBaseKey(template: PromptTemplateItem): string | null {
  const suffix = template.scope === 'system' ? SYSTEM_TEMPLATE_KEYS[template.id] : DOMAIN_TEMPLATE_KEYS[template.id];
  if (!suffix) return null;
  return template.scope === 'system'
    ? `promptTemplate.systemBuiltIns.${suffix}`
    : `promptTemplate.domainBuiltIns.${suffix}`;
}

export function promptTemplateVersionsDisplay(
  template: PromptTemplateItem,
  versions: VersionItem[],
  t: Translate,
): VersionItem[] {
  const baseKey = templateBaseKey(template);
  if (!baseKey) return versions;
  return versions.map((version) => ({
    ...version,
    content: t(`${baseKey}.content`),
  }));
}

/**
 * Localize product-owned seed templates by exact stable ID.
 * Customer-created templates use different IDs and are always displayed verbatim.
 */
export function systemPromptTemplateDisplay(template: PromptTemplateItem, t: Translate): PromptTemplateItem {
  const baseKey = templateBaseKey(template);
  if (!baseKey) return template;
  const versions = template.versions_json || [];
  const localizedVersions = promptTemplateVersionsDisplay(template, versions, t);

  return {
    ...template,
    name: t(`${baseKey}.name`),
    description: t(`${baseKey}.description`),
    created_by: t('promptTemplate.systemUser'),
    versions_json: localizedVersions,
  };
}
