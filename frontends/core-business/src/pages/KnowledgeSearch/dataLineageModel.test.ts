import { describe, expect, it } from 'vitest';

import { hasLineage, lineageSummary } from './dataLineageModel';

describe('dataLineageModel', () => {
  it('hasLineage false for empty', () => {
    expect(hasLineage(undefined)).toBe(false);
    expect(hasLineage({ items: [] } as any)).toBe(false);
  });

  it('unknown renders explicitly', () => {
    const s = lineageSummary({
      items: [
        {
          entity_type: 'Job',
          table: 'dbo.jobs',
          upstream: [],
          downstream: [],
          unknown: true,
        },
      ],
    } as any);
    expect(s[0].unknown).toBe(true);
  });

  it('counts edges', () => {
    const s = lineageSummary({
      items: [
        {
          entity_type: 'Job',
          table: 't',
          upstream: [{ endpoint: 'a' }],
          downstream: [{ endpoint: 'b' }, { endpoint: 'c' }],
          unknown: false,
        },
      ],
    } as any);
    expect(s[0]).toMatchObject({ upstream: 1, downstream: 2 });
  });
});
