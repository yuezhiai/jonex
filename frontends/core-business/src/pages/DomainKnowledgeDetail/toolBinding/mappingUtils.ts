import type { BindingMappingProvenance } from '@/types/domainKnowledge';

export type MappingSource = 'input' | 'attr' | 'entity' | 'literal';
export type LiteralType = 'string' | 'number' | 'boolean' | 'null';
export type MappingRowError = 'param_required' | 'value_required' | 'invalid_number';

export interface MappingRow {
  id: string;
  param: string;
  source: MappingSource;
  sourceValue: string;
  literalType: LiteralType;
  provenance?: BindingMappingProvenance;
  confidence?: number;
  reason?: string;
}

/** 解析参数映射 JSON：空 → {}；不是对象 → null（校验失败）。 */
export function parseMapping(raw?: string): Record<string, unknown> | null {
  const text = (raw || '').trim();
  if (!text) return {};
  try {
    const value = JSON.parse(text);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

/** 参数映射回填到文本框：空映射给空串（与新建一致）。 */
export function mappingToText(mapping?: Record<string, unknown> | null): string {
  return mapping && Object.keys(mapping).length ? JSON.stringify(mapping, null, 2) : '';
}

/** 两份映射是否相同（按键排序后比较，避免只是键顺序不同也当成修改）。 */
export function sameMapping(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const norm = (m: Record<string, unknown>) =>
    JSON.stringify(Object.keys(m).sort().map((k) => [k, m[k]]));
  return norm(a) === norm(b);
}

export function splitMappingSource(value: unknown): { source: MappingSource; value: string } {
  if (typeof value === 'string' && value.startsWith('$input.')) {
    return { source: 'input', value: value.slice('$input.'.length) };
  }
  if (typeof value === 'string' && value.startsWith('$attr.')) {
    return { source: 'attr', value: value.slice('$attr.'.length) };
  }
  if (value === '$entity.name') {
    return { source: 'entity', value: '' };
  }
  return {
    source: 'literal',
    value: typeof value === 'string' ? value : JSON.stringify(value),
  };
}

export function literalTypeOf(value: unknown): LiteralType {
  if (value === null) return 'null';
  if (typeof value === 'number') return 'number';
  if (typeof value === 'boolean') return 'boolean';
  return 'string';
}

export function joinMappingSource(source: MappingSource, value: string): unknown {
  if (source === 'input') return `$input.${value}`;
  if (source === 'attr') return `$attr.${value}`;
  if (source === 'entity') return '$entity.name';
  return value;
}

const NUMBER_RE = /^-?(?:\d+|\d*\.\d+)(?:[eE][+-]?\d+)?$/;

export function convertLiteral(
  raw: string,
  type: LiteralType,
): { ok: true; value: unknown } | { ok: false } {
  if (type === 'string') return { ok: true, value: raw };
  if (type === 'null') return { ok: true, value: null };
  if (type === 'boolean') return { ok: true, value: raw === 'true' };
  const text = raw.trim();
  if (!NUMBER_RE.test(text)) return { ok: false };
  return { ok: true, value: Number(text) };
}

export function rowsFromMapping(
  mapping: Record<string, unknown>,
  nextId: () => string,
): MappingRow[] {
  return Object.entries(mapping).map(([param, raw]) => {
    const split = splitMappingSource(raw);
    return {
      id: nextId(),
      param,
      source: split.source,
      sourceValue: split.value,
      literalType: literalTypeOf(raw),
      provenance: 'user',
    };
  });
}

export function serializeRows(rows: MappingRow[]): {
  mapping: Record<string, unknown>;
  errors: Record<string, MappingRowError>;
} {
  const mapping: Record<string, unknown> = {};
  const errors: Record<string, MappingRowError> = {};
  for (const row of rows) {
    const key = row.param.trim();
    if (!key) {
      errors[row.id] = 'param_required';
      continue;
    }
    if ((row.source === 'input' || row.source === 'attr') && !row.sourceValue.trim()) {
      errors[row.id] = 'value_required';
      continue;
    }
    if (row.source === 'literal') {
      const converted = convertLiteral(row.sourceValue, row.literalType);
      if (!converted.ok) {
        errors[row.id] = 'invalid_number';
        continue;
      }
      mapping[key] = converted.value;
    } else {
      mapping[key] = joinMappingSource(row.source, row.sourceValue);
    }
  }
  return { mapping, errors };
}
