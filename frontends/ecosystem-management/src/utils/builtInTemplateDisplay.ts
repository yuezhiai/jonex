import type {
  TemplateAttribute,
  TemplateDomain,
  TemplateObject,
  TemplateRelation,
  TemplateScenario,
} from '../api/templateScenarios';

type Translate = (key: string, options?: Record<string, unknown>) => string;

const BUILT_IN_DOMAIN_KEYS: Record<string, string> = {
  tpl_domain_ai_tech_report: 'aiTechReport',
  tpl_domain_hardware_internet: 'hardwareInternet',
  tpl_domain_manufacturing: 'manufacturing',
  tpl_domain_medical: 'healthcare',
  tpl_domain_finance: 'financialServices',
  tpl_domain_internet: 'internetTechnology',
};

const BUILT_IN_SCENARIO_KEYS: Record<string, string> = {
  tpl_scenario_internet_general: 'internetGeneral',
  tpl_scenario_credit_risk: 'creditRisk',
  tpl_scenario_robo_advisor: 'investmentAdvisory',
  tpl_scenario_medical_record: 'medicalRecord',
  tpl_scenario_quality_inspection: 'qualityInspection',
  tpl_scenario_hw_inet_finance: 'financialReport',
  tpl_scenario_llm_tech_report: 'llmTechnicalReport',
};

const ACRONYMS: Record<string, string> = {
  ai: 'AI',
  api: 'API',
  cad: 'CAD',
  cpu: 'CPU',
  esg: 'ESG',
  gpu: 'GPU',
  id: 'ID',
  iot: 'IoT',
  json: 'JSON',
  kpi: 'KPI',
  llm: 'LLM',
  mqtt: 'MQTT',
  ocr: 'OCR',
  pdf: 'PDF',
  rl: 'RL',
  rnd: 'R&D',
  url: 'URL',
  xml: 'XML',
};

function humanizeOntologyCode(code: string): string {
  return code
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      return ACRONYMS[lower] || `${lower.charAt(0).toUpperCase()}${lower.slice(1)}`;
    })
    .join(' ');
}

export function isEnglishLanguage(language?: string): boolean {
  return Boolean(language?.toLowerCase().startsWith('en'));
}

export function getTemplateDomainDisplay(domain: TemplateDomain, t: Translate, english = false) {
  const builtInKey = BUILT_IN_DOMAIN_KEYS[domain.id];
  return {
    name: english ? (domain.name_en || domain.name) : domain.name,
    description: builtInKey
      ? t(`templateDomains.builtIn.${builtInKey}.description`)
      : (domain.description || ''),
  };
}

export function getTemplateScenarioDisplay(scenario: TemplateScenario, english: boolean, t: Translate) {
  const key = english ? BUILT_IN_SCENARIO_KEYS[scenario.id] : undefined;
  return key
    ? {
        name: t(`templateScenarios.builtIn.${key}.name`),
        description: t(`templateScenarios.builtIn.${key}.description`),
      }
    : { name: scenario.name, description: scenario.description || '' };
}

export function getTemplateObjectDisplay(object: TemplateObject, english: boolean) {
  const builtIn = english && /^(tpl_obj_|tpl_object_)/.test(object.id) && object.ontology_code;
  const name = builtIn ? humanizeOntologyCode(object.ontology_code as string) : object.name;
  return {
    name,
    description: builtIn ? `Built-in object definition for ${name}.` : object.description || '',
  };
}

export function getTemplateAttributeDisplay(attribute: TemplateAttribute, english: boolean) {
  const builtIn = english && /^tpl_attr(?:ibute)?_/.test(attribute.id) && attribute.ontology_code;
  const name = builtIn ? humanizeOntologyCode(attribute.ontology_code as string) : attribute.attr_name;
  return {
    name,
    description: builtIn ? `Built-in attribute definition for ${name}.` : attribute.description || '',
  };
}

export function getTemplateRelationDisplay(
  relation: TemplateRelation,
  english: boolean,
  sourceName?: string,
  targetName?: string,
) {
  const builtIn = english && /^(tpl_rel_|tpl_relation_)/.test(relation.id) && relation.ontology_code;
  const name = builtIn ? humanizeOntologyCode(relation.ontology_code as string) : relation.name;
  return {
    name,
    description: builtIn
      ? `Defines a relationship from ${sourceName || 'the source object'} to ${targetName || 'the target object'}.`
      : relation.description || '',
  };
}
