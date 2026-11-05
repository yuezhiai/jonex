/**
 * 数据源展示/筛选的纯逻辑回归。
 *
 * 锁两件事：
 *  1. `excludeEtlSources` 只剔除 `etl`，api / storage / file / database 原样保留
 *     —— 多源视图、实体落点、指标落点三个消费面共用它，任何一类被误伤都会
 *     让真实数据源从界面上消失。
 *  2. `etl` 有本地化映射：KB 卡片标签来自 `knowledge_info.data_source_types`，
 *     漏映射会露出原始串 `etl`。
 */
import { describe, expect, it } from 'vitest';

import { accessTypeDisplayName, compiledSchemaSources, excludeEtlSources } from './dataSourceDisplay';

const t = (key: string) => key;

describe('excludeEtlSources', () => {
  it('剔除 ETL 来源，其余 accessType 原样保留且顺序不变', () => {
    const list = [
      { id: 'ds_etl', accessType: 'etl' },
      { id: 'ds_api', accessType: 'api' },
      { id: 'ds_storage', accessType: 'storage' },
      { id: 'ds_push', accessType: 'api_push' },
      { id: 'ds_file', accessType: 'file' },
      { id: 'ds_db', accessType: 'database' },
    ];
    expect(excludeEtlSources(list).map((ds) => ds.id)).toEqual([
      'ds_api', 'ds_storage', 'ds_push', 'ds_file', 'ds_db',
    ]);
  });

  it('没有 ETL 来源时返回等价的列表', () => {
    const list = [{ id: 'ds_db', accessType: 'database' }];
    expect(excludeEtlSources(list)).toEqual(list);
  });
});

describe('compiledSchemaSources', () => {
  it('只保留 database 源，file/api/storage/api_push/etl 全部剔除', () => {
    const list = [
      { id: 'ds_db', accessType: 'database' },
      { id: 'ds_file', accessType: 'file' },
      { id: 'ds_api', accessType: 'api' },
      { id: 'ds_storage', accessType: 'storage' },
      { id: 'ds_push', accessType: 'api_push' },
      { id: 'ds_etl', accessType: 'etl' },
    ];
    expect(compiledSchemaSources(list).map((ds) => ds.id)).toEqual(['ds_db']);
  });

  it('没有 database 源时返回空列表', () => {
    expect(compiledSchemaSources([{ accessType: 'file' }])).toEqual([]);
  });
});

describe('accessTypeDisplayName', () => {
  it('etl 走本地化 key，不露出原始串', () => {
    expect(accessTypeDisplayName('etl', t)).toBe('domainKnowledge.dataSourceType.etl');
  });
});
