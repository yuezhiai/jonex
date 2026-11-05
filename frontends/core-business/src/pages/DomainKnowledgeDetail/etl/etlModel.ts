export type EtlKind = 'file' | 'openlineage' | 'openmetadata' | 'datahub'

export function isEtlSource(ds: { accessType?: string }): boolean {
  return ds?.accessType === 'etl'
}

export function etlKindLabelKey(kind?: string): string {
  if (kind === 'openlineage') return 'domainKnowledge.etl.kindOpenlineage'
  if (kind === 'openmetadata') return 'domainKnowledge.etl.kindOpenmetadata'
  if (kind === 'datahub') return 'domainKnowledge.etl.kindDatahub'
  if (kind === 'file') return 'domainKnowledge.etl.kindFile'
  // 未知/缺失的 kind 不能冒充「文件导入」：后端将来加档位时要能一眼看出来。
  return 'domainKnowledge.etl.kindUnknown'
}

export function canImportFile(ds: { configJson?: Record<string, unknown> }): boolean {
  return ds?.configJson?.etl_kind === 'file'
}

export interface EtlConfigInput {
  data_source_id: string
  etl_kind: EtlKind
  /** 仅 OpenLineage 用：事件 dataset namespace 白名单 */
  namespace?: string
}

/**
 * 新建 ETL 来源时提交的 `config_json`（与后端 `normalize_etl_config` 同口径）。
 *
 * 命名空间**只对 OpenLineage 生效**，且空白不写这个键 —— 后端按「不限制
 * namespace」处理；写个空串反而会把 `""` 当成不存在的白名单值传下去。
 */
export function buildEtlConfig(v: EtlConfigInput): Record<string, string> {
  const out: Record<string, string> = {
    data_source_id: v.data_source_id,
    etl_kind: v.etl_kind,
  }
  const ns = (v.namespace || '').trim()
  if (v.etl_kind === 'openlineage' && ns) out.namespace = ns
  return out
}
