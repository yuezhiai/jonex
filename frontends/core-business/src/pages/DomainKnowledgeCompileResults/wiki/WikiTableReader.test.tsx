// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { WikiTableData } from '@/types/domainKnowledge';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/api/wikiResources', () => ({ lookupWikiRecords: vi.fn() }));
vi.mock('antd', () => ({
  Alert: ({ title }: any) => <div>{title}</div>,
  Button: ({ children, onClick, disabled }: any) => <button onClick={onClick} disabled={disabled}>{children}</button>,
  Empty: () => <div />,
  Modal: ({ open, children, footer }: any) => open ? <div>{children}{footer}</div> : null,
  Select: ({ value, onChange, options }: any) => <select value={value} onChange={e => onChange(e.target.value)}>
    {(options || []).map((item: any) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>,
  Spin: ({ children }: any) => <div>{children}</div>,
  Tabs: () => <div />,
}));

import { lookupWikiRecords } from '@/api/wikiResources';
import WikiTableReader from './WikiTableReader';

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Wiki table paging', () => {
  it('uses the pinned revision and advances through the server cursor', async () => {
    const reply = (row: number, next: string | null) => ({
      revision: 'rev-1', review_version: 0, gaps: [], fields: [{ label: 'Value', column_index: 1 }],
      records: [{ row_id: row, document_id: 'doc', table_id: 'table-1',
        cells: [{ row_index: row, column_index: 1, coordinate: `A${row}`, raw_value: `value-${row}` }] }],
      total: 100, has_more: Boolean(next), next_cursor: next, coverage: {}, context: {}, observations: null,
    });
    vi.mocked(lookupWikiRecords).mockResolvedValueOnce(reply(1, 'cursor-2') as any)
      .mockResolvedValueOnce(reply(51, null) as any);
    const data = { schema_version: 1, revision: 'rev-1', document_id: 'doc', source_id: 'src', selected_table_id: 'table-1',
      complete: false, sheets: [{ sheet_id: 'sheet', name: 'Sheet', visibility: 'visible', tables: [{
        table_id: 'table-1', sheet_id: 'sheet', title: 'Table', structure_status: 'accepted', rows: [], records: [], dimensions: [],
        bounds: { row_start: 1, row_end: 100, column_start: 1, column_end: 1 }, shown_row_end: 50,
        has_dates: false, date_inferred: false, complete: false,
      }] }] } as WikiTableData;
    render(<WikiTableReader kbId="kb-1" data={data} fileName="source.xlsx" onOpenSource={() => {}} />);
    fireEvent.click(screen.getByText('wikiTableReading.pagedTable'));
    await screen.findByText('value-1');
    expect(lookupWikiRecords).toHaveBeenNthCalledWith(1, 'kb-1', expect.objectContaining({ revision: 'rev-1', cursor: '' }));
    fireEvent.click(screen.getByText('wikiTableReading.nextPage'));
    await waitFor(() => expect(screen.getByText('value-51')).toBeTruthy());
    expect(lookupWikiRecords).toHaveBeenNthCalledWith(2, 'kb-1', expect.objectContaining({ revision: 'rev-1', cursor: 'cursor-2' }));
  });
});
