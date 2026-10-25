import { describe, expect, it } from 'vitest';

import { toAttributeSourceView } from './attributeSourceView.js';
import type { EntityLanding } from '../domain/index.js';

const landing = (dataSourceId: string, sourceName: string | null): EntityLanding => ({
  dataSourceId, entityName: 'X', included: true, sourceName,
});

describe('toAttributeSourceView', () => {
  it('源显示名从所属实体的落点里解析（同源即同名）', () => {
    const view = toAttributeSourceView(
      { dataSourceId: 'ds1', entityName: 'Users', attributeName: 'id', stale: false, staleReason: null },
      [landing('ds1', 'jinpin')],
    );
    expect(view.sourceName).toBe('jinpin');
    expect(view.column).toBe('Users.id');
    expect(view.stale).toBe(false);
  });

  it('落点里没有该源 + source_gone → sourceGone=true（界面回落「已被删除」文案，不摊 id）', () => {
    const view = toAttributeSourceView(
      { dataSourceId: 'ds9', entityName: 'T', attributeName: 'c', stale: true, staleReason: 'source_gone' },
      [],
    );
    expect(view.sourceName).toBeUndefined();
    expect(view.sourceGone).toBe(true);
    expect(view.stale).toBe(true);
  });

  it('落点里没有该源且非 source_gone → 照旧回落 raw dataSourceId（至少可对账）', () => {
    const view = toAttributeSourceView(
      { dataSourceId: 'ds2', entityName: 'T', attributeName: 'c', stale: false, staleReason: null },
      [],
    );
    expect(view.sourceGone).toBe(false);
    expect(view.dataSourceId).toBe('ds2');
  });
});
