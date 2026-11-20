import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WikiSourceImage } from '@/types/domainKnowledge';

vi.mock('./request', () => ({
  request: { post: vi.fn(() => Promise.resolve({})) },
  getData: (result: Promise<unknown>) => result,
}));

import { request } from './request';
import { findWikiImages, inspectWikiImage } from './wikiImages';

describe('Wiki image resource requests', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('passes the KB as a query parameter and search options as the request body', async () => {
    await findWikiImages('kb-a', { query: 'chart', document_id: 'doc-a' });
    expect(request.post).toHaveBeenCalledWith('/knowledge-base/wiki/resources/find_images',
      { query: 'chart', document_id: 'doc-a', limit: 12 },
      { params: { knowledge_base_id: 'kb-a' } });
  });

  it('passes the fixed revision and image identity for visual inspection', async () => {
    const image = { bundle_sha: 'revision', document_id: 'doc-a', asset_id: 'image-a' } as WikiSourceImage;
    await inspectWikiImage('kb-a', image, 'What is shown?');
    expect(request.post).toHaveBeenCalledWith('/knowledge-base/wiki/resources/inspect_image',
      { revision: 'revision', document_id: 'doc-a', asset_id: 'image-a', question: 'What is shown?' },
      { params: { knowledge_base_id: 'kb-a' }, timeout: 180_000 });
  });
});
