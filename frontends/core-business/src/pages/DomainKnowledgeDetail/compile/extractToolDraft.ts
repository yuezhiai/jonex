// 抽取期工具声明（catalog: 技能）的表单草稿 ⇄ 声明转换与校验。
// 校验口径与后端 jonex_core/common/catalog_skill_spec.py 一致；后端保存时还会再校验一遍。
import type { ExtractionSkillRef, ExtractionSkillsConfig } from '@/types/domainKnowledge';

export type ExtractGroup = 'refine' | 'constrain';
export type InputSource = 'name' | 'type' | 'attr' | 'literal';
export type EqualsKind = 'false' | 'true' | 'string' | 'number' | 'null';

export interface InputDraft {
  param: string;
  source: InputSource;
  /** source=attr：属性键 */
  attr?: string;
  /** source=literal：固定值（按字符串传） */
  literal?: string;
}

export interface WriteDraft {
  attr: string;
  path: string;
}

export interface CatalogDraft {
  key: string;
  group: ExtractGroup;
  tool: string;
  description: string;
  inputs: InputDraft[];
  writes: WriteDraft[];
  rejectPath: string;
  equalsKind: EqualsKind;
  equalsValue: string;
  onError: string;
}

/** 校验错误：i18n 键（compile.extractTool.err.*）+ 参数 */
export interface DraftError {
  code: string;
  params?: Record<string, string | number>;
}

export const CATALOG_PREFIX = 'catalog:';
export const CONTRACT_FIELDS = [
  'preconditions',
  'authorization',
  'atomic_effects',
  'follow_ups',
  'failure_policy',
  'audit_required',
];
export const MAX_ENTRIES = 20;
const TOOL_RE = /^[A-Za-z0-9_-]{1,128}$/;
const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const PATH_RE = /^\$(\.[^.[\]\s]+|\[\d+\])*$/;
const ATTR_REF_RE = /^\$attr\.([A-Za-z_][A-Za-z0-9_]{0,63})$/;

let seq = 0;
export function newDraftKey(): string {
  seq += 1;
  return `decl_${Date.now().toString(36)}_${seq}`;
}

export function emptyDraft(group: ExtractGroup = 'refine'): CatalogDraft {
  return {
    key: newDraftKey(),
    group,
    tool: '',
    description: '',
    inputs: [],
    writes: group === 'refine' ? [{ attr: '', path: '$.data.' }] : [],
    rejectPath: '$.data.',
    equalsKind: 'false',
    equalsValue: '',
    onError: 'keep',
  };
}

export function isCatalogSkill(skill?: string): boolean {
  return typeof skill === 'string' && skill.trim().startsWith(CATALOG_PREFIX);
}

/** 用户少写了 `$` 前缀时补上：`data.x` → `$.data.x` */
export function normalizePath(path: string): string {
  const p = (path || '').trim();
  if (!p) return p;
  if (p.startsWith('$')) return p;
  return p.startsWith('.') || p.startsWith('[') ? `$${p}` : `$.${p}`;
}

function inputToDraft(param: string, value: unknown): InputDraft {
  if (value === '$entity.name') return { param, source: 'name' };
  if (value === '$entity.type') return { param, source: 'type' };
  if (typeof value === 'string') {
    const m = ATTR_REF_RE.exec(value);
    if (m) return { param, source: 'attr', attr: m[1] };
  }
  return { param, source: 'literal', literal: value === undefined || value === null ? '' : String(value) };
}

function refToDraft(group: ExtractGroup, ref: ExtractionSkillRef): CatalogDraft {
  const rw = ref.reject_when;
  let equalsKind: EqualsKind = 'string';
  let equalsValue = '';
  if (rw) {
    if (rw.equals === null) equalsKind = 'null';
    else if (rw.equals === true) equalsKind = 'true';
    else if (rw.equals === false) equalsKind = 'false';
    else if (typeof rw.equals === 'number') {
      equalsKind = 'number';
      equalsValue = String(rw.equals);
    } else equalsValue = String(rw.equals ?? '');
  } else {
    equalsKind = 'false';
  }
  return {
    key: newDraftKey(),
    group,
    tool: ref.skill.trim().slice(CATALOG_PREFIX.length),
    description: ref.description || '',
    inputs: Object.entries(ref.inputs || {}).map(([k, v]) => inputToDraft(k, v)),
    writes: Object.entries(ref.writes || {}).map(([attr, path]) => ({ attr, path })),
    rejectPath: rw?.path || '$.data.',
    equalsKind,
    equalsValue,
    onError: ref.on_error || 'keep',
  };
}

/** 把一份 extraction_skills 拆成「内置（只读，原样保留）」与「catalog: 草稿」。 */
export function splitSkills(cfg?: ExtractionSkillsConfig | null): {
  builtin: { refine: ExtractionSkillRef[]; constrain: ExtractionSkillRef[] };
  drafts: CatalogDraft[];
} {
  const builtin = { refine: [] as ExtractionSkillRef[], constrain: [] as ExtractionSkillRef[] };
  const drafts: CatalogDraft[] = [];
  (['refine', 'constrain'] as const).forEach((group) => {
    (cfg?.[group] || []).forEach((ref) => {
      if (!ref || typeof ref !== 'object') return;
      if (isCatalogSkill(ref.skill)) drafts.push(refToDraft(group, ref));
      else builtin[group].push(ref);
    });
  });
  return { builtin, drafts };
}

/** 一条草稿 → 声明；有错误时 ref 为 null。 */
export function draftToRef(d: CatalogDraft): { ref: ExtractionSkillRef | null; errors: DraftError[] } {
  const errors: DraftError[] = [];
  const tool = (d.tool || '').trim();
  if (!tool) errors.push({ code: 'toolRequired' });
  else if (!TOOL_RE.test(tool)) errors.push({ code: 'toolInvalid', params: { tool } });

  const inputs: Record<string, string> = {};
  if (d.inputs.length > MAX_ENTRIES) errors.push({ code: 'tooMany', params: { max: MAX_ENTRIES } });
  d.inputs.forEach((row) => {
    const param = (row.param || '').trim();
    if (!KEY_RE.test(param)) {
      errors.push({ code: 'paramInvalid', params: { name: param || '—' } });
      return;
    }
    if (param in inputs) {
      errors.push({ code: 'paramDuplicate', params: { name: param } });
      return;
    }
    if (row.source === 'name') inputs[param] = '$entity.name';
    else if (row.source === 'type') inputs[param] = '$entity.type';
    else if (row.source === 'attr') {
      const attr = (row.attr || '').trim();
      if (!KEY_RE.test(attr)) {
        errors.push({ code: 'inputAttrInvalid', params: { name: param } });
        return;
      }
      inputs[param] = `$attr.${attr}`;
    } else {
      const literal = row.literal ?? '';
      // 以 $ 开头的字面量会被当成取值来源，后端会拒；这里先拦
      if (literal.startsWith('$')) {
        errors.push({ code: 'literalDollar', params: { name: param } });
        return;
      }
      inputs[param] = literal;
    }
  });

  const ref: ExtractionSkillRef = { skill: `${CATALOG_PREFIX}${tool}` };
  if (d.description.trim()) ref.description = d.description.trim();
  if (Object.keys(inputs).length) ref.inputs = inputs;

  if (d.group === 'refine') {
    const writes: Record<string, string> = {};
    const rows = d.writes.filter((w) => (w.attr || '').trim() || (w.path || '').trim().replace(/^\$\.?/, ''));
    if (!rows.length) errors.push({ code: 'writesRequired' });
    if (rows.length > MAX_ENTRIES) errors.push({ code: 'tooMany', params: { max: MAX_ENTRIES } });
    rows.forEach((w) => {
      const attr = (w.attr || '').trim();
      const path = normalizePath(w.path);
      if (!KEY_RE.test(attr) || attr.startsWith('_')) {
        errors.push({ code: 'writeAttrInvalid', params: { name: attr || '—' } });
        return;
      }
      if (CONTRACT_FIELDS.includes(attr)) {
        errors.push({ code: 'writeContract', params: { name: attr } });
        return;
      }
      if (attr in writes) {
        errors.push({ code: 'writeDuplicate', params: { name: attr } });
        return;
      }
      if (!PATH_RE.test(path) || path === '$' || path.endsWith('.')) {
        errors.push({ code: 'pathInvalid', params: { name: attr } });
        return;
      }
      writes[attr] = path;
    });
    ref.writes = writes;
    ref.on_error = d.onError === 'drop_attr' ? 'drop_attr' : 'keep';
  } else {
    const path = normalizePath(d.rejectPath);
    if (!PATH_RE.test(path) || path === '$' || path.endsWith('.')) {
      errors.push({ code: 'rejectPathInvalid' });
    }
    let equals: string | number | boolean | null = null;
    if (d.equalsKind === 'true') equals = true;
    else if (d.equalsKind === 'false') equals = false;
    else if (d.equalsKind === 'number') {
      const n = Number(d.equalsValue);
      if (d.equalsValue.trim() === '' || !Number.isFinite(n)) errors.push({ code: 'equalsNumber' });
      equals = n;
    } else if (d.equalsKind === 'string') equals = d.equalsValue;
    ref.reject_when = { path, equals };
    ref.on_error = d.onError === 'reject' ? 'reject' : 'keep';
  }
  return { ref: errors.length ? null : ref, errors };
}

/** 内置技能原样在前，catalog: 声明按草稿顺序接在各自分组后面；两组都空时返回 null。 */
export function mergeSkills(
  builtin: { refine: ExtractionSkillRef[]; constrain: ExtractionSkillRef[] },
  drafts: CatalogDraft[],
): { config: ExtractionSkillsConfig | null; errors: { index: number; errors: DraftError[] }[] } {
  const refine = [...builtin.refine];
  const constrain = [...builtin.constrain];
  const failed: { index: number; errors: DraftError[] }[] = [];
  drafts.forEach((d, index) => {
    const { ref, errors } = draftToRef(d);
    if (!ref) {
      failed.push({ index, errors });
      return;
    }
    (d.group === 'refine' ? refine : constrain).push(ref);
  });
  const config: ExtractionSkillsConfig = {};
  if (refine.length) config.refine = refine;
  if (constrain.length) config.constrain = constrain;
  return { config: Object.keys(config).length ? config : null, errors: failed };
}

/** 与 api/domainKnowledge.ts 里新属性的键推导一致：空白与斜杠换下划线，去掉其它符号。 */
export function attributeKeyOf(name: string): string {
  return (name || '')
    .trim()
    .replace(/[\s/\\]+/g, '_')
    .replace(/[^a-zA-Z0-9_\u4e00-\u9fff]/g, '');
}

/** 按工具入参 schema 生成默认入参行：第一项取实体名，与本对象属性同名的取属性，其它留固定值待填。 */
export function defaultInputsFor(
  inputSchema: Record<string, any> | undefined,
  attrKeys: string[],
): InputDraft[] {
  const props = Object.keys((inputSchema && inputSchema.properties) || {});
  return props.map((param, i) => {
    if (attrKeys.includes(param)) return { param, source: 'attr', attr: param };
    if (i === 0) return { param, source: 'name' };
    return { param, source: 'literal', literal: '' };
  });
}
