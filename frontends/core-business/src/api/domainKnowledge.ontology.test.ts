import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./request', () => ({
  request: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  getData: vi.fn(),
  postData: vi.fn(),
  putData: vi.fn(),
  deleteData: vi.fn(),
}));

import { putData } from './request';
import { updateOntologyInstance } from './domainKnowledge';

beforeEach(() => {
  vi.resetAllMocks();
});

const KB = 'kb_001';

describe('updateOntologyInstance merge_target', () => {
  it('默认请求体只带 updates，不夹带 merge_target', async () => {
    await updateOntologyInstance(KB, 'Organization', '麦瑞', { name: '迈瑞' });

    expect(putData).toHaveBeenCalledWith(
      `/knowledge-base/ontology/instances/Organization/${encodeURIComponent('麦瑞')}?knowledge_base_id=${KB}`,
      { updates: { name: '迈瑞' } },
    );
  });

  it('用户确认合并后显式带上 merge_target=true', async () => {
    await updateOntologyInstance(KB, 'Organization', '麦瑞', { name: '迈瑞' }, true);

    expect(putData).toHaveBeenCalledWith(
      `/knowledge-base/ontology/instances/Organization/${encodeURIComponent('麦瑞')}?knowledge_base_id=${KB}`,
      { updates: { name: '迈瑞' }, merge_target: true },
    );
  });
});
