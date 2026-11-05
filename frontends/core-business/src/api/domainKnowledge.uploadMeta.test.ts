import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./request', () => ({
  request: {
    post: vi.fn(() =>
      Promise.resolve({ data: { success: true, data: { id: 'd1', file_name: 'a.txt' } } }),
    ),
  },
  getData: vi.fn((p: Promise<unknown>) => p),
  postData: vi.fn(),
  putData: vi.fn(),
  deleteData: vi.fn(),
}));

import { request } from './request';
import { uploadManualDocument } from './domainKnowledge';

function formOfCall(index = 0): FormData {
  return (request.post as unknown as { mock: { calls: unknown[][] } }).mock.calls[index][1] as FormData;
}

describe('uploadManualDocument metadata', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('拒绝作为新版本时把目标写入 similarity_suppressed_targets', async () => {
    await uploadManualDocument('kb1', new File(['x'], 'a.txt'), undefined, undefined, {
      suppressSimilarityTargetId: 'doc-old',
    });
    const form = formOfCall();
    expect(form.get('metadata')).toBe('{"similarity_suppressed_targets":["doc-old"]}');
  });

  it('没有抑制目标时不 append metadata', async () => {
    await uploadManualDocument('kb1', new File(['x'], 'a.txt'));
    const form = formOfCall();
    expect(form.get('metadata')).toBeNull();
  });

  it('FormData 仍包含 file / knowledge_base_id / folder_id', async () => {
    await uploadManualDocument('kb1', new File(['x'], 'a.txt'), 'folder-1');
    const form = formOfCall();
    expect((form.get('file') as File).name).toBe('a.txt');
    expect(form.get('knowledge_base_id')).toBe('kb1');
    expect(form.get('folder_id')).toBe('folder-1');
  });
});
