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

import { getData, postData, request } from './request';
import {
  dismissSimilaritySuggestion,
  getDomainKnowledgeDetail,
  getSimilaritySuggestions,
  mapBackendDoc,
  mergeSimilaritySuggestion,
} from './domainKnowledge';

beforeEach(() => {
  vi.resetAllMocks();
});

describe('mapBackendDoc similarity_hint', () => {
  it('映射后端 similarity_hint 为 camelCase', () => {
    const doc = mapBackendDoc(
      {
        id: 'doc-1',
        file_name: 'a.pdf',
        status: 'ready',
        similarity_hint: {
          target_document_id: 'tgt-1',
          target_file_name: 'old.pdf',
          similarity: 0.93,
        },
      },
      'kb-1',
      (key) => key,
    );
    expect(doc.similarityHint).toEqual({
      targetDocumentId: 'tgt-1',
      targetFileName: 'old.pdf',
      similarity: 0.93,
    });
  });

  it('后端缺失 similarity_hint 时返回 null', () => {
    const doc = mapBackendDoc(
      { id: 'doc-2', file_name: 'b.pdf', status: 'ready' },
      'kb-1',
      (key) => key,
    );
    expect(doc.similarityHint).toBeNull();
  });

  it('后端显式 similarity_hint: null 时返回 null', () => {
    const doc = mapBackendDoc(
      { id: 'doc-3', file_name: 'c.pdf', status: 'ready', similarity_hint: null },
      'kb-1',
      (key) => key,
    );
    expect(doc.similarityHint).toBeNull();
  });
});

describe('getDomainKnowledgeDetail similarity check fields', () => {
  it('映射 similarity_check_enabled / similarity_check_threshold', async () => {
    vi.mocked(getData).mockResolvedValue({
      id: 'kb-1',
      tenant_id: 't',
      space_id: 's-1',
      name: 'KB',
      description: null,
      data_source_types: ['file'],
      document_count: 2,
      status: 'synced',
      owner_id: null,
      created_at: null,
      updated_at: null,
      similarity_check_enabled: true,
      similarity_check_threshold: 0.92,
    });

    const detail = await getDomainKnowledgeDetail('kb-1');
    expect(detail.similarityCheckEnabled).toBe(true);
    expect(detail.similarityCheckThreshold).toBe(0.92);
  });

  it('后端显式 similarity_check_enabled: false 时保持 false', async () => {
    vi.mocked(getData).mockResolvedValue({
      id: 'kb-1b',
      tenant_id: 't',
      space_id: 's-1',
      name: 'KB1b',
      description: null,
      data_source_types: ['file'],
      document_count: 0,
      status: 'synced',
      owner_id: null,
      created_at: null,
      updated_at: null,
      similarity_check_enabled: false,
      similarity_check_threshold: null,
    });

    const detail = await getDomainKnowledgeDetail('kb-1b');
    expect(detail.similarityCheckEnabled).toBe(false);
    expect(detail.similarityCheckThreshold).toBeNull();
  });

  it('后端缺省时为 false / null', async () => {
    vi.mocked(getData).mockResolvedValue({
      id: 'kb-2',
      tenant_id: 't',
      space_id: 's-1',
      name: 'KB2',
      description: null,
      data_source_types: ['file'],
      document_count: 0,
      status: 'synced',
      owner_id: null,
      created_at: null,
      updated_at: null,
    });

    const detail = await getDomainKnowledgeDetail('kb-2');
    expect(detail.similarityCheckEnabled).toBe(false);
    expect(detail.similarityCheckThreshold).toBeNull();
  });
});

describe('getSimilaritySuggestions', () => {
  it('映射 camelCase，source_block_reason 透传', async () => {
    vi.mocked(getData).mockResolvedValue({
      items: [
        {
          id: 'sg-1',
          target_document_id: 'tgt-1',
          target_file_name: 'old.pdf',
          similarity: 0.91,
          method: 'text_lines',
          can_merge: true,
          block_reason: null,
          source_block_reason: 'document_busy',
        },
      ],
    });

    const items = await getSimilaritySuggestions('doc-1');

    expect(request.get).toHaveBeenCalledWith(
      '/knowledge-base/documents/doc-1/similarity-suggestions',
    );
    expect(items).toEqual([
      {
        id: 'sg-1',
        targetDocumentId: 'tgt-1',
        targetFileName: 'old.pdf',
        similarity: 0.91,
        method: 'text_lines',
        canMerge: true,
        blockReason: null,
        sourceBlockReason: 'document_busy',
      },
    ]);
  });

  it('canMerge=true 且两个 reason 均为 null 时原样返回', async () => {
    vi.mocked(getData).mockResolvedValue({
      items: [
        {
          id: 'sg-3',
          target_document_id: 'tgt-3',
          target_file_name: 'same.pdf',
          similarity: 1,
          method: 'text_lines',
          can_merge: true,
          block_reason: null,
          source_block_reason: null,
        },
      ],
    });

    const [item] = await getSimilaritySuggestions('doc-3');
    expect(item.canMerge).toBe(true);
    expect(item.blockReason).toBeNull();
    expect(item.sourceBlockReason).toBeNull();
  });

  it('空列表返回空数组', async () => {
    vi.mocked(getData).mockResolvedValue({ items: [] });
    await expect(getSimilaritySuggestions('doc-2')).resolves.toEqual([]);
  });

  it('缺省字段兜底：similarity 0、method 空串、canMerge false', async () => {
    vi.mocked(getData).mockResolvedValue({
      items: [{ id: 'sg-2', target_document_id: 'tgt-2', similarity: 0 }],
    });
    const [item] = await getSimilaritySuggestions('doc-2');
    expect(item.targetFileName).toBe('');
    expect(item.similarity).toBe(0);
    expect(item.method).toBe('');
    expect(item.canMerge).toBe(false);
    expect(item.blockReason).toBeNull();
    expect(item.sourceBlockReason).toBeNull();
  });

  it('丢弃缺 id / target_document_id 的脏条目', async () => {
    vi.mocked(getData).mockResolvedValue({
      items: [
        { id: '', target_document_id: 'tgt-a', similarity: 0.9 },
        { id: 'sg-b', target_document_id: '', similarity: 0.9 },
        { id: 'sg-c', target_document_id: 'tgt-c', similarity: 0.9 },
      ],
    });
    const items = await getSimilaritySuggestions('doc-4');
    expect(items).toHaveLength(1);
    expect(items[0].id).toBe('sg-c');
  });
});

describe('mergeSimilaritySuggestion', () => {
  it('返回映射后的目标文档与版本号', async () => {
    vi.mocked(postData).mockResolvedValue({
      target_document_id: 'tgt-1',
      version_no: 3,
      reparse_error: 'reparse failed',
    });

    const result = await mergeSimilaritySuggestion('doc-1', 'sg-1');

    expect(postData).toHaveBeenCalledWith(
      '/knowledge-base/documents/doc-1/similarity-suggestions/sg-1/merge',
      {},
    );
    expect(result).toEqual({
      targetDocumentId: 'tgt-1',
      versionNo: 3,
      reparseError: 'reparse failed',
    });
  });
});

describe('dismissSimilaritySuggestion', () => {
  it('调用 dismiss 路径', async () => {
    vi.mocked(postData).mockResolvedValue(undefined);
    await dismissSimilaritySuggestion('doc-1', 'sg-1');
    expect(postData).toHaveBeenCalledWith(
      '/knowledge-base/documents/doc-1/similarity-suggestions/sg-1/dismiss',
      {},
    );
  });
});
