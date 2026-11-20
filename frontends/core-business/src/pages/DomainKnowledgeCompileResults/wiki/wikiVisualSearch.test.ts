import { describe, expect, it } from 'vitest';
import { visualMatch } from './wikiVisualSearch';

describe('visual image candidates', () => {
  it('requires an actual image receipt and an explicit leading decision', () => {
    expect(visualMatch(true, 'MATCH: a chart')).toBe('MATCH');
    expect(visualMatch(true, 'NO_MATCH: a map')).toBe('NO_MATCH');
    expect(visualMatch(true, 'UNCERTAIN: blurred')).toBe('UNCERTAIN');
    expect(visualMatch(false, 'MATCH: a chart')).toBe('UNCERTAIN');
    expect(visualMatch(true, 'There may be a MATCH')).toBe('UNCERTAIN');
  });
});
