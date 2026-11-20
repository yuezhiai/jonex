import { describe, expect, it, vi } from 'vitest';
import { consumeWikiSearchStream } from './wikiSearchStream';

describe('Wiki query completion metadata', () => {
  it('keeps partial status and source scope when the stream completes', async () => {
    const outcomes = [{ kb_id: 'kb', incomplete: true, stop_reason: 'context_window_limit' }];
    const event = { answer: 'A supported partial answer.', incomplete: true, query_outcomes: outcomes };
    const response = new Response(`event: complete\ndata: ${JSON.stringify(event)}\n\n`);
    const onDelta = vi.fn();
    const onDone = vi.fn();
    await consumeWikiSearchStream(response, { onDelta, onDone });
    expect(onDelta).toHaveBeenCalledWith(event.answer, expect.objectContaining({ incomplete: true, query_outcomes: outcomes }));
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ incomplete: true, query_outcomes: outcomes }));
  });
});
