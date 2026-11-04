// @vitest-environment jsdom
/**
 * 数据路径「跳过模拟打字」回归（2026-10-13 数据问答展示优化）。
 *
 * 数据路径（`source === 'data'` 或非空 `data_blocks`）的正文与结构化块是 router
 * 纯 Python 一次性渲染的产物（非 LLM 流式生成）：逐字「打字」没有语义，且正文
 * 可达数千字（50 行表格 + SQL）要打几十秒 —— 断言一次性交付（onDelta 恰一次、
 * 参数即完整正文、meta 带 data_blocks，紧随 onDone）。
 *
 * RAG / 本体路径（无 data_blocks 且 `source !== 'data'`）的打字观感是产品行为，
 * 本次改动不得回归：断言多次 onDelta、各段拼接 == 完整正文、onDone 收尾。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@jonex/shell-sdk', () => ({ readAccessToken: () => 'test-token' }));

import { streamKnowledgeSearch } from './knowledgeSearch';
import type {
  DataBlock,
  KnowledgeSearchStreamHandlers,
  KnowledgeSearchStreamMeta,
} from '../types/knowledgeSearch';

function jsonResponse(payload: unknown): Response {
  return { status: 200, ok: true, json: async () => payload } as unknown as Response;
}

function dataBlock(): DataBlock {
  return {
    data_source_id: 'ds_assets',
    source_name: '资产库',
    engine: 'nl2sql',
    kind: 'answer',
    text: '**count** = 17\n\n引擎：NL→SQL（②）',
    table: { columns: ['count'], rows: [[17]] },
  };
}

function collectors() {
  const deltas: Array<{ delta: string; meta?: KnowledgeSearchStreamMeta }> = [];
  const dones: Array<KnowledgeSearchStreamMeta | undefined> = [];
  const errors: Error[] = [];
  const handlers: KnowledgeSearchStreamHandlers = {
    onDelta: (delta, meta) => deltas.push({ delta, meta }),
    onDone: (meta) => dones.push(meta),
    onError: (error) => errors.push(error),
  };
  return { deltas, dones, errors, handlers };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('本体+数据 / 纯数据两条路线', () => {
  it('ontologyData=true 时请求 /knowledge-base/search/ontology-data', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', ontologyData: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/ontology-data');
    expect(url).not.toContain('/knowledge-base/search/data');
  });

  it('data=true 时请求 /knowledge-base/search/data（纯数据档不动）', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', data: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/data');
  });

  it('两个布尔位同时为 true 时「本体 + 数据」优先（ontologyData wins）', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', ontologyData: true, data: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/ontology-data');
    expect(url).not.toContain('/knowledge-base/search/data');
  });

  it('前端选择位不下发：body 里没有 data / ontologyData 键（只是路由开关）', async () => {
    for (const selector of [{ ontologyData: true }, { data: true }]) {
      fetchMock.mockClear();
      fetchMock.mockResolvedValue(
        jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
      );

      const { handlers } = collectors();
      await streamKnowledgeSearch({ query: 'q', ...selector }, handlers);

      const init = fetchMock.mock.calls[0][1] as { body?: string };
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      // 选择位只在前端换算成端点，绝不能出现在请求体里（后端 DTO 没有这两个键）
      expect(body).not.toHaveProperty('data');
      expect(body).not.toHaveProperty('ontologyData');
    }
  });

  it('deep 优先于本体+数据：{ontologyData, deep} → /knowledge-base/search/deep', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', ontologyData: true, deep: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/deep');
    expect(url).not.toContain('/knowledge-base/search/ontology-data');
  });

  it('wiki 优先于本体+数据：{ontologyData, wiki} → /knowledge-base/search/jonex_wiki', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', ontologyData: true, wiki: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/jonex_wiki');
    expect(url).not.toContain('/knowledge-base/search/ontology-data');
  });

  it('本体+数据优先于纯数据：{ontologyData, data} → /knowledge-base/search/ontology-data', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [] } }),
    );

    const { handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', ontologyData: true, data: true }, handlers);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/knowledge-base/search/ontology-data');
    expect(url).not.toContain('/knowledge-base/search/data');
  });

  it('data_references 透出到 meta', async () => {
    const refs = [{ source_name: '财报Mock库', sql: 'SELECT 1', table: { columns: ['a'], rows: [[1]] } }];
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答案', source: 'ontology', references: [], data_references: refs } }),
    );

    const { deltas, dones, handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q', data: true }, handlers);

    expect(deltas[0].meta?.data_references).toEqual(refs);
    expect(dones[0]?.data_references).toEqual(refs);
  });
});

describe('数据路径跳过模拟打字（一次性交付）', () => {
  it('source=data：onDelta 恰 1 次且参数 = 完整 answer，meta 带 data_blocks，随后 onDone', async () => {
    // 长正文：旧实现逐字打要 25ms × (len/2) —— 断言调用次数即可，不依赖计时
    const answer = '公司共 17 家。' + '明细见下表：'.repeat(30);
    const blocks = [dataBlock()];
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'data', data_blocks: blocks, references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '有多少家公司' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas).toHaveLength(1);
    expect(deltas[0].delta).toBe(answer);
    expect(deltas[0].meta?.source).toBe('data');
    // 表格随 meta 即渲染的前提：这一次 onDelta 的 meta 就带着结构化块
    expect(deltas[0].meta?.data_blocks).toEqual(blocks);
    expect(dones).toHaveLength(1);
    expect(dones[0]?.data_blocks).toEqual(blocks);
  });

  it('source 未标 data 但 data_blocks 非空：同样一次性交付（判据是 OR）', async () => {
    const answer = '共 17 家。' + '点开可看明细。'.repeat(20);
    const blocks = [dataBlock()];
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'fanout', data_blocks: blocks, references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '有多少家公司' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas).toHaveLength(1);
    expect(deltas[0].delta).toBe(answer);
    expect(deltas[0].meta?.data_blocks).toEqual(blocks);
    expect(dones).toHaveLength(1);
  });

  it('source=ontology + data_supplement=true：保留打字路径，meta 带补充标记与数据块', async () => {
    const answer = '文档答案。';
    const blocks = [dataBlock()];
    fetchMock.mockResolvedValue(
      jsonResponse({
        success: true,
        data: { answer, source: 'ontology', data_blocks: blocks, data_supplement: true, references: [] },
      }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '成都宏科电子生产哪些产品？' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas.length).toBeGreaterThan(1);          // 仍逐字交付
    expect(deltas.map((d) => d.delta).join('')).toBe(answer);
    expect(deltas[0].meta?.data_supplement).toBe(true);
    expect(deltas[0].meta?.data_blocks).toEqual(blocks);
    expect(dones).toHaveLength(1);
  });

  it('无 data_supplement 时维持旧判据：非空 data_blocks 仍一次性交付', async () => {
    const answer = '共 17 家。';
    const blocks = [dataBlock()];
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'ontology', data_blocks: blocks } }),
    );

    const { deltas, handlers } = collectors();
    await streamKnowledgeSearch({ query: 'q' }, handlers);
    expect(deltas).toHaveLength(1);
  });

  it('仅 source=data、无 data_blocks 键（DI 单次路径 / 回退关档形态）：同样一次性交付', async () => {
    // fanout.render_di_answer 返回 kind "data"，router._merge 不带 blocks ⇒ 响应里
    // data_blocks 键整个不存在 —— 这是跳过打字实际生效的主要分支之一。
    const answer = '共 17 家公司。' + '（DI 单次路径答案）'.repeat(20);
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'data', references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '有多少家公司' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas).toHaveLength(1);
    expect(deltas[0].delta).toBe(answer);
    expect(deltas[0].meta?.source).toBe('data');
    expect(deltas[0].meta?.data_blocks).toBeNull();
    expect(dones).toHaveLength(1);
    expect(dones[0]?.data_blocks).toBeNull();
  });

  it('已中止的 signal（数据路径）：onDelta / onDone 均不派发（中止契约不变）', async () => {
    const controller = new AbortController();
    controller.abort();
    const answer = '共 17 家。' + '明细见下表：'.repeat(10);
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'data', data_blocks: [dataBlock()], references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '有多少家公司' }, handlers, controller.signal);

    expect(errors).toEqual([]);
    expect(deltas).toEqual([]);
    expect(dones).toEqual([]);
  });
});

describe('[A] 血缘解释块元数据映射（data_lineage → meta.data_lineage）', () => {
  it('响应带 data_lineage：onDelta / onDone 的 meta 都原样带上（组件渲染的前提）', async () => {
    const lineage = {
      items: [
        {
          entity_type: 'Job',
          data_source_id: 'ds_assets',
          table: 'dbo.jobs',
          upstream: [{ endpoint: 'dbo.src', column: 'id' }],
          downstream: [],
          unknown: true,
        },
      ],
    };
    fetchMock.mockResolvedValue(
      jsonResponse({
        success: true,
        data: { answer: '公司共 17 家。', source: 'data', data_lineage: lineage, references: [] },
      }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '有多少家公司' }, handlers);

    expect(errors).toEqual([]);
    // 删掉 knowledgeSearch.ts:417 的 data_lineage 映射，这两条断言即红
    expect(deltas[0].meta?.data_lineage).toEqual(lineage);
    expect(dones[0]?.data_lineage).toEqual(lineage);
  });

  it('响应无 data_lineage 键（旧后端）：meta 落 null，渲染零回归', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer: '答。', source: 'data', references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '问题' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas[0].meta?.data_lineage).toBeNull();
    expect(dones[0]?.data_lineage).toBeNull();
  });
});

describe('非数据路径打字保留（零回归）', () => {
  it('RAG 路径（无 data_blocks）：onDelta 多次、分片拼接 == 完整 answer、onDone 收尾', async () => {
    const answer = 'RAG 路径逐字输出。';
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'rag', references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '问题' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas.length).toBeGreaterThan(1);
    expect(deltas.map((d) => d.delta).join('')).toBe(answer);
    // 每一片都 <= 2 字符（模拟打字的既有步长）
    expect(deltas.every((d) => d.delta.length <= 2)).toBe(true);
    expect(deltas.every((d) => (d.meta?.data_blocks ?? null) === null)).toBe(true);
    expect(dones).toHaveLength(1);
  });

  it('空 data_blocks 数组不算数据路径（边界）：打字保留', async () => {
    const answer = '空块数组仍打字。';
    fetchMock.mockResolvedValue(
      jsonResponse({ success: true, data: { answer, source: 'rag', data_blocks: [], references: [] } }),
    );

    const { deltas, dones, errors, handlers } = collectors();
    await streamKnowledgeSearch({ query: '问题' }, handlers);

    expect(errors).toEqual([]);
    expect(deltas.length).toBeGreaterThan(1);
    expect(deltas.map((d) => d.delta).join('')).toBe(answer);
    expect(dones).toHaveLength(1);
  });
});
