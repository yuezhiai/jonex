import { expect, it } from 'vitest';
import { toSpanRequest } from './documentBBox';

it('maps a table_row location to query fields', () => {
  expect(toSpanRequest({
    type: 'table_row', chunk_index: 7, page_no: 2,
    table_idx: 1, row_start: 3, row_end: 5,
  } as any)).toEqual({
    chunkIndex: 7,
    expect: { page_no: 2, table_idx: 1, row_start: 3, row_end: 5 },
  });
});

it('drops null/undefined fields', () => {
  expect(toSpanRequest({ type: 'page', chunk_index: 2, page_no: null } as any))
    .toEqual({ chunkIndex: 2, expect: {} });
});
