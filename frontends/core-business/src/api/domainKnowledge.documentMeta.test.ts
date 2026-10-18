import { describe, expect, it } from 'vitest';
import { mapBackendDoc } from './domainKnowledge';

describe('mapBackendDoc', () => {
  it('暴露 metadata.ingest_backend', () => {
    const doc = mapBackendDoc(
      {
        id: 'doc-1',
        file_name: 'a.pdf',
        status: 'ready',
        metadata: { ingest_backend: 'rag', kb_type: 'lightrag' },
      },
      'kb-1',
      (key) => key,
    );
    expect(doc.ingestBackend).toBe('rag');
    expect(doc.kbType).toBe('lightrag');
  });

  it('老文档没有粘性值时保持 undefined（按 rag 口径）', () => {
    const doc = mapBackendDoc(
      { id: 'doc-2', file_name: 'b.pdf', status: 'ready', metadata: {} },
      'kb-1',
      (key) => key,
    );
    expect(doc.ingestBackend).toBeUndefined();
  });
});
