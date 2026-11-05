import { describe, expect, it } from 'vitest'
import { isEtlSource, etlKindLabelKey, canImportFile, buildEtlConfig } from './etlModel'

describe('etlModel', () => {
  it('filters only etl sources', () => {
    expect(isEtlSource({ accessType: 'etl' } as any)).toBe(true)
    expect(isEtlSource({ accessType: 'database' } as any)).toBe(false)
  })
  it('maps known kinds to i18n keys and unknown kinds to an explicit fallback', () => {
    expect(etlKindLabelKey('file')).toBe('domainKnowledge.etl.kindFile')
    expect(etlKindLabelKey('openlineage')).toBe('domainKnowledge.etl.kindOpenlineage')
    expect(etlKindLabelKey('openmetadata')).toBe('domainKnowledge.etl.kindOpenmetadata')
    expect(etlKindLabelKey('datahub')).toBe('domainKnowledge.etl.kindDatahub')
    // 未知/缺失不能冒充「文件导入」——后端新增档位时要能一眼看见
    expect(etlKindLabelKey('dbt')).toBe('domainKnowledge.etl.kindUnknown')
    expect(etlKindLabelKey(undefined)).toBe('domainKnowledge.etl.kindUnknown')
  })
  it('only file kind can import', () => {
    expect(canImportFile({ configJson: { etl_kind: 'file' } } as any)).toBe(true)
    expect(canImportFile({ configJson: { etl_kind: 'openlineage' } } as any)).toBe(false)
    expect(canImportFile({ configJson: { etl_kind: 'dbt' } } as any)).toBe(false)
    expect(canImportFile({ configJson: {} } as any)).toBe(false)
  })
})

describe('buildEtlConfig', () => {
  it('openlineage 带上去空白的命名空间', () => {
    expect(buildEtlConfig({
      data_source_id: 'ds_1', etl_kind: 'openlineage', namespace: '  jonex-wte-etl  ',
    })).toEqual({
      data_source_id: 'ds_1', etl_kind: 'openlineage', namespace: 'jonex-wte-etl',
    })
  })
  it('命名空间留空时不写该键（后端按「不限」处理）', () => {
    expect(buildEtlConfig({
      data_source_id: 'ds_1', etl_kind: 'openlineage', namespace: '   ',
    })).toEqual({ data_source_id: 'ds_1', etl_kind: 'openlineage' })
    expect(buildEtlConfig({
      data_source_id: 'ds_1', etl_kind: 'openlineage',
    })).toEqual({ data_source_id: 'ds_1', etl_kind: 'openlineage' })
  })
  it('file 类型即使填了也不提交（该字段只对 OpenLineage 生效）', () => {
    expect(buildEtlConfig({
      data_source_id: 'ds_1', etl_kind: 'file', namespace: 'jonex-wte-etl',
    })).toEqual({ data_source_id: 'ds_1', etl_kind: 'file' })
  })
  it('OpenMetadata / DataHub 是平台推送，命名空间一律不提交', () => {
    for (const kind of ['openmetadata', 'datahub'] as const) {
      expect(buildEtlConfig({
        data_source_id: 'ds_1', etl_kind: kind, namespace: 'jonex-wte-etl',
      })).toEqual({ data_source_id: 'ds_1', etl_kind: kind })
    }
  })
})
