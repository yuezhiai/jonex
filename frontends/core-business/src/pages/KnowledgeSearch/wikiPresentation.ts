import type { WikiProgressDetails, WikiProgressEvent } from '@/types/knowledgeSearch';

/** Add layout breaks only when the answer already contains the complete, ordered section labels. */
export function formatWikiAnswer(content: string): string {
  if (!content || /```|~~~/.test(content)) return content;
  const markers = [...content.matchAll(/(^|\n|(?<=[。.!?])\s+)(Evidence|Interpretation|Conclusion|证据|解读|解释|结论)\s*[:：]/g)];
  const kinds = markers.map((m) => ['Evidence', '证据'].includes(m[2]) ? 'evidence'
    : ['Conclusion', '结论'].includes(m[2]) ? 'conclusion' : 'interpretation');
  if (markers.length !== 3 || kinds.join(',') !== 'evidence,interpretation,conclusion'
      || content.slice(0, markers[0].index).trim()) return content;
  let formatted = content;
  for (let i = markers.length - 1; i >= 0; i--) {
    const marker = markers[i];
    const at = marker.index!;
    const label = marker[0].slice(marker[1].length);
    formatted = formatted.slice(0, at) + `\n\n### ${label}\n\n` + formatted.slice(at + marker[0].length);
  }
  return formatted.trim();
}

export interface WikiProgressEntry {
  id: string;
  category: 'search' | 'read' | 'table' | 'analyze' | 'answer';
  done: boolean;
  durationMs?: number;
  details?: WikiProgressDetails;
}

/** A tool's start and completion become one row; each row still represents a real public event. */
export function wikiProgressEntries(events: WikiProgressEvent[]): WikiProgressEntry[] {
  const entries: WikiProgressEntry[] = [];
  const pending = new Map<string, number>();
  // Polling may combine libraries by timestamp; sequence is authoritative within each library.
  const libraries = new Map<string, WikiProgressEvent[]>();
  for (const event of events) {
    const key = event.kb_id ?? '';
    const group = libraries.get(key) ?? [];
    group.push(event);
    libraries.set(key, group);
  }
  for (const event of [...libraries.values()].flatMap((group) => group.sort((a, b) => a.seq - b.seq))) {
    if (!event.tool && event.type !== 'model' && event.type !== 'synthesis') continue;
    const category = event.tool?.startsWith('table_') ? 'table'
      : event.tool?.includes('search') ? 'search'
      : event.tool ? 'read' : event.type === 'synthesis' ? 'answer' : 'analyze';
    const key = `${event.kb_id ?? ''}:${event.tool ?? event.type}`;
    const finished = event.duration_ms != null;
    const index = pending.get(key);
    if (finished && index != null) {
      entries[index] = { ...entries[index], done: true, durationMs: event.duration_ms,
        details: event.tool ? { ...entries[index].details, ...event.details } : undefined };
      pending.delete(key);
    } else {
      entries.push({ id: `${key}:${event.seq}:${entries.length}`, category,
        done: finished, durationMs: event.duration_ms, details: event.tool ? event.details : undefined });
      if (!finished) pending.set(key, entries.length - 1);
    }
  }
  return entries;
}
