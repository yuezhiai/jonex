import { describe, expect, it } from 'vitest';

import { canOpenSourceCanvas, sourceCanvasPath } from './landings.js';

describe('sourceCanvasPath', () => {
  it('拼出数据源级画布路由（与 DataSourceTab 的跳转同口径）', () => {
    expect(sourceCanvasPath('kb_1', 'ds_1')).toBe('/domain-knowledge/kb_1/schema/ds_1');
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
