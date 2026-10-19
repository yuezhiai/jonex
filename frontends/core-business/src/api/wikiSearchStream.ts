import type { KnowledgeSearchStreamHandlers } from '@/types/knowledgeSearch';

/** Incremental SSE parsing; handles UTF-8 and event frames split across network chunks. */
export async function consumeWikiSearchStream(response: Response, handlers: KnowledgeSearchStreamHandlers, onSession?: (id: string) => void) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Wiki progress stream is unavailable');
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;
  const frame = (value: string) => {
    let event = '';
    const data: string[] = [];
    for (const line of value.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    const payload = JSON.parse(data.join('\n'));
    if (event === 'progress') {
      if (payload.session_id) onSession?.(payload.session_id);
      handlers.onProgress?.(payload.events ?? []);
    }
    if (event === 'error') throw new Error(payload.message || 'Wiki search failed');
    if (event === 'complete') {
      completed = true;
      const meta = { source: payload.source, references: payload.references ?? [],
        reasoning: payload.reasoning ?? null, rag_used: payload.rag_used, history_id: payload.history_id };
      // Deliver the verified answer immediately; no synthetic typing delay.
      handlers.onDelta(payload.answer ?? '', meta);
      handlers.onDone?.(meta);
    }
  };
  try {
    while (!completed) {
      const { value, done } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n');
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        frame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) break;
    }
    if (!completed) throw new Error('Wiki progress stream ended before the answer completed');
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
