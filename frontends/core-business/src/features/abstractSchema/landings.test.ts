import { describe, expect, it } from 'vitest';

import { canOpenSourceCanvas, excludeLanding, sourceCanvasPath } from './landings.js';

describe('sourceCanvasPath', () => {
  it('拼出数据源级画布路由（与 DataSourceTab 的跳转同口径）', () => {
    expect(sourceCanvasPath('kb_1', 'ds_1')).toBe('/domain-knowledge/kb_1/schema/ds_1');
  });
});

describe('excludeLanding', () => {
  it('排除落点改为保留并置 included=false', () => {
    const next = excludeLanding(
      [{ dataSourceId: 'd1', entityName: 'asset', included: true, sourceName: null }],
      'd1', 'asset',
    );
    expect(next).toEqual([
      { dataSourceId: 'd1', entityName: 'asset', included: false, sourceName: null },
    ]);
  });

  it('只命中 (ds, entity) 那一条，其余原样返回（不删行）', () => {
    const keep = { dataSourceId: 'd2', entityName: 'asset', included: true, sourceName: 'B' };
    const target = { dataSourceId: 'd1', entityName: 'asset', included: true, sourceName: 'A' };
    const next = excludeLanding([target, keep], 'd1', 'asset');
    expect(next).toHaveLength(2);
    expect(next[0]).toEqual({ ...target, included: false });
    expect(next[1]).toBe(keep);   // 未命中的行不重建
    expect(target.included).toBe(true);  // 纯函数：不改入参
  });
});

describe('canOpenSourceCanvas', () => {
  it('源已被删（source_gone）→ 不给入口（点进去只会回落知识库级，误导）', () => {
    expect(canOpenSourceCanvas({ staleReason: 'source_gone' })).toBe(false);
  });

  it('实体/属性失效或未标脏 → 照常可开（画布本身能看该源现状）', () => {
    expect(canOpenSourceCanvas({ staleReason: 'entity_gone' })).toBe(true);
    expect(canOpenSourceCanvas({ staleReason: null })).toBe(true);
    expect(canOpenSourceCanvas({})).toBe(true);
  });
});
