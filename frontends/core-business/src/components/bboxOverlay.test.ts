import { describe, expect, it } from 'vitest';
import { bboxPct, pickPrimarySpan, resolveTargetPage, shouldUseBboxViewer } from './bboxOverlay';

describe('bboxPct', () => {
  it('converts 0-1000 top-left to percentages', () => {
    expect(bboxPct([100, 200, 300, 400])).toMatchObject({
      left: '10%', top: '20%', width: '20%', height: '20%',
    });
  });
  it('flips y for bottom-left origin', () => {
    expect(bboxPct([0, 100, 100, 200], 'bottom-left')).toMatchObject({ top: '80%', height: '10%' });
  });
});

describe('primary span', () => {
  const spans = [
    { page_idx: 0, bbox: [1, 1, 2, 2], ordinal: 0 },
    { page_idx: 3, bbox: [1, 1, 2, 2], ordinal: 1 },
    { page_idx: 3, bbox: [3, 3, 4, 4], ordinal: 2 },
  ];
  it('prefers the citation page over a header span on another page', () => {
    expect(resolveTargetPage(spans, 4)).toBe(4);
    expect(pickPrimarySpan(spans, 4)?.ordinal).toBe(1);
  });
  it('falls back to the smallest page when the citation page has no span', () => {
    expect(resolveTargetPage(spans, 9)).toBe(1);
    expect(pickPrimarySpan(spans, 9)?.ordinal).toBe(0);
  });
});

describe('viewer fallback', () => {
  it('renders bbox only when there is at least one span', () => {
    expect(shouldUseBboxViewer([])).toBe(false);
    expect(shouldUseBboxViewer([{ page_idx: 0, bbox: [0, 0, 1, 1] }])).toBe(true);
  });
});
