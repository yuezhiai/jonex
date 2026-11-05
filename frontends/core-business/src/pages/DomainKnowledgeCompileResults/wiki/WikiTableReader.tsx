import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, Select, Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import type { WikiTableData, WikiTableRecord } from '@/types/domainKnowledge';
import './WikiTableReader.scss';

type Entry = WikiTableRecord & { sheetId: string; sheetName: string; tableId: string };
type Mode = 'date' | 'group' | 'raw';

/** The display angles never own a second copy of the facts. */
export default function WikiTableReader({ data, fileName, onOpenSource }: {
  data: WikiTableData; fileName: string; onOpenSource: () => void;
}) {
  const { t } = useTranslation();
  const tr = (key: string) => t(`wikiTableReading.${key}`);
  const initialSheet = data.sheets.find(s => s.tables.some(table => table.table_id === data.selected_table_id));
  const [sheetId, setSheetId] = useState(initialSheet?.sheet_id || data.sheets[0]?.sheet_id || '');
  const [mode, setMode] = useState<Mode>(initialSheet?.tables.some(table => table.has_dates) ? 'date' : 'raw');
  const [axis, setAxis] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Entry | null>(null);
  const [coordinate, setCoordinate] = useState('');
  const container = useRef<HTMLDivElement>(null);
  const sheets = useMemo(() => data.sheets.filter(s => !sheetId || s.sheet_id === sheetId), [data, sheetId]);
  const tables = useMemo(() => sheets.flatMap(sheet => sheet.tables.map(table => ({ ...table, sheet }))), [sheets]);
  const entries = useMemo<Entry[]>(() => tables.filter(table => table.structure_status === 'accepted').flatMap(table =>
    table.records.map(record => ({ ...record, sheetId: table.sheet.sheet_id, sheetName: table.sheet.name, tableId: table.table_id }))), [tables]);
  const dimensions = useMemo(() => {
    const result = new Map<string, { label: string; key: string; values: string[] }>();
    tables.forEach(table => table.dimensions.forEach(d => {
      const id = `${d.key}:${d.label}`;
      const existing = result.get(id);
      result.set(id, { key: id, label: d.label, values: [...new Set([...(existing?.values || []), ...d.values])] });
    }));
    return [...result.values()];
  }, [tables]);
  const currentAxis = dimensions.find(d => d.key === axis) || dimensions[dimensions.length - 1];
  const dimensionValue = (entry: Entry) => entry.dimensions.find(d => `${d.key}:${d.label}` === currentAxis?.key)?.value || '';
  const hasDates = entries.some(entry => entry.date);
  const activeMode = mode === 'date' && !hasDates ? 'raw' : mode === 'group' && !currentAxis ? 'raw' : mode;
  const filtered = entries.filter(entry => Object.entries(filters).every(([key, value]) => !value || entry.dimensions.some(d => `${d.key}:${d.label}` === key && d.value === value)));
  const visibleEntries = filtered.filter(entry => activeMode !== 'date' || entry.date);
  const visibleTables = new Set(visibleEntries.map(entry => entry.tableId));
  const context = [...new Set(tables.filter(table => activeMode === 'raw' || visibleTables.has(table.table_id)).flatMap(table => table.context || []))];
  const groups = new Map<string, Entry[]>();
  visibleEntries.forEach(entry => {
    const title = activeMode === 'date' ? entry.date?.value : dimensionValue(entry);
    if (title) groups.set(title, [...(groups.get(title) || []), entry]);
  });
  const groupList = [...groups.entries()].sort(([a], [b]) => activeMode === 'date' ? a.localeCompare(b) : 0);
  useEffect(() => {
    if (activeMode !== 'raw' && selected && !visibleEntries.some(entry => entry.id === selected.id)) setSelected(null);
  }, [activeMode, selected, visibleEntries]);
  useEffect(() => {
    if (activeMode !== 'raw' || !coordinate) return;
    container.current?.querySelector<HTMLElement>(`[data-coordinate="${coordinate}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [activeMode, sheetId, coordinate]);
  const showRaw = () => {
    if (selected) { setSheetId(selected.sheetId); setCoordinate(selected.coordinate); }
    setMode('raw');
  };
  const chooseSheet = (value: string) => { setSheetId(value); setFilters({}); setSelected(null); setCoordinate(''); };
  return <div className="wiki-table-reader" ref={container}>
    <Tabs activeKey={activeMode} onChange={key => setMode(key as Mode)} items={[
      { key: 'date', label: tr('byDate'), disabled: !hasDates },
      { key: 'group', label: tr('byCategory'), disabled: !currentAxis },
      { key: 'raw', label: tr('originalSheets') },
    ]} />
    <div className="wiki-table-reader__toolbar">
      <label>{tr('worksheet')}<Select showSearch optionFilterProp="label" aria-label={tr('worksheet')} value={sheetId} onChange={chooseSheet} options={[
        { value: '', label: tr('allSheets') }, ...data.sheets.map(sheet => ({ value: sheet.sheet_id, label: sheet.name })),
      ]} /></label>
      {activeMode === 'group' && currentAxis && <label>{tr('dimension')}<Select showSearch optionFilterProp="label" aria-label={tr('dimension')} value={currentAxis.key} onChange={setAxis}
        options={dimensions.map(d => ({ value: d.key, label: d.label }))} /></label>}
      {activeMode !== 'raw' && dimensions.map(d => <label key={d.key}>{d.label}<Select showSearch optionFilterProp="label" aria-label={d.label}
        value={filters[d.key] || ''} onChange={value => setFilters(previous => ({ ...previous, [d.key]: value }))} options={[
          { value: '', label: tr('all') }, ...d.values.map(value => ({ value, label: value })),
        ]} /></label>)}
    </div>
    {context.length > 0 && <Alert type="info" title={context.join(' · ')} />}
    {tables.some(table => table.date_inferred) && <Alert type="warning" showIcon title={tr('inferredDate')} />}
    {(!data.complete || tables.some(table => !table.complete)) && <Alert type="info" showIcon title={tr('partial')} />}
    <div className="wiki-table-reader__layout">
      <div className="wiki-table-reader__content">
        {activeMode === 'raw' ? tables.map(table => <section key={table.table_id}>
          <h3>{table.sheet.name}{table.sheet.tables.length > 1 ? ` · ${table.title}` : ''}</h3>
          {table.structure_status !== 'accepted' && <Alert type="warning" title={tr('ambiguous')} />}
          <div className="wiki-table-reader__grid"><table><thead><tr><th>{tr('row')}</th>
            {table.rows[0]?.cells.map(cell => <th key={cell.column}>{cell.coordinate.match(/^([A-Z]+)[0-9]+$/)?.[1] || `C${cell.column}`}</th>)}
          </tr></thead><tbody>{table.rows.map(row => <tr key={row.row}><th>{row.row}</th>{row.cells.map(cell =>
            <td key={cell.coordinate} className={coordinate === cell.coordinate ? 'is-selected' : ''}>
              <button data-coordinate={cell.coordinate} title={`${cell.coordinate}${cell.anchor ? ` ← ${cell.anchor}` : ''}${cell.hidden ? ` (${tr('hidden')})` : ''}${cell.number_format ? ` · ${cell.number_format}` : ''}`}
                onClick={() => { setCoordinate(cell.coordinate); setSelected({ id: cell.coordinate, row: row.row, coordinate: cell.coordinate,
                  value: cell.value, date: null, dimensions: [], measures: [], sheetId: table.sheet.sheet_id, sheetName: table.sheet.name, tableId: table.table_id }); }}>
                {cell.formula && cell.cached_value == null ? tr('unresolvedFormula') : cell.value || <span className="wiki-table-reader__empty">—</span>}
              </button>
            </td>)}</tr>)}</tbody></table></div>
        </section>) : groupList.length ? groupList.map(([title, records]) => <section key={title}>
          <h3>{title}</h3><div className="wiki-table-reader__records">{records.map(entry => <div key={entry.id} className="wiki-table-reader__record">
            <div className="wiki-table-reader__record-context">{activeMode === 'group' ? entry.date?.value : entry.dimensions.map(d => d.value).filter(Boolean).join(' / ')}
              {!sheetId && <small>{entry.sheetName}</small>}</div>
            <button className={selected?.id === entry.id ? 'is-selected' : ''} onClick={() => setSelected(entry)}>{entry.value}</button>
          </div>)}</div>
        </section>) : <Empty description={tr('noRecords')} />}
      </div>
      <aside className="wiki-table-reader__source" aria-label={tr('sourceAndLocation')}>
        <h3>{tr('sourceAndLocation')}</h3><dl><dt>{tr('sourceFile')}</dt><dd>{fileName || data.document_id}</dd>
          {selected ? <><dt>{tr('worksheet')}</dt><dd>{selected.sheetName}</dd><dt>{tr('selectedValue')}</dt><dd>{selected.value}</dd>
            <dt>{tr('sourceCell')}</dt><dd><button onClick={showRaw}>{selected.coordinate}</button></dd>
            {selected.date && <><dt>{tr('dateSource')}</dt><dd>{selected.date.coordinate}: {selected.date.raw_value}
              {selected.date.status === 'candidate' && <small>{tr('candidate')}</small>}</dd></>}
            {selected.dimensions.filter(d => d.value).map(d => <React.Fragment key={d.key}><dt>{d.label}</dt><dd>{d.value} <small>{d.coordinate}</small></dd></React.Fragment>)}
            {selected.measures.map(d => <React.Fragment key={d.key}><dt>{d.label}</dt><dd>{d.value || tr('unfilled')} <small>{d.coordinate}</small></dd></React.Fragment>)}
          </> : <dd className="wiki-table-reader__hint">{tr('selectValue')}</dd>}</dl>
        {selected && <Button type="link" onClick={showRaw}>{tr('showOriginal')}</Button>}
        <Button type="link" onClick={onOpenSource}>{tr('openSource')}</Button>
        <p className="wiki-table-reader__hint">{tr('sourceLabels')}</p>
      </aside>
    </div>
  </div>;
}
