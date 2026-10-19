import { describe, expect, it } from 'vitest';
import { preferredLocation } from './citationIndex';
import type { KnowledgeReference } from '@/types/knowledgeSearch';

/** 构造一条引用（只填 preferredLocation 关心的字段） */
function ref(
  media_type: KnowledgeReference['media_type'],
  locations: KnowledgeReference['locations'],
): KnowledgeReference {
  return { doc_id: 'doc-1', file_name: 'a.pdf', media_type, locations };
}

describe('preferredLocation', () => {
  it('本体路径按文档聚合的多 location 引用：定位跟着展示正文所在 location，而不是首个页码 location', () => {
    // 线上实测形状（2026-10-19「美国时薪同比分析」）：
    // 答案 [1] 引的是图表描述（image chunk 4，page_no=2 → PDF 第 3 页），
    // 但同一文档还有一条更靠前的 page location（page_no=1 → PDF 第 2 页）。
    // 旧逻辑按「首个 page location」定位 → 跳到了上一页的无关正文。
    const reference = ref('pdf', [
      {
        type: 'image', chunk_index: 4, image_idx: 0, page_no: 2,
        text: '这张折线图展示了2017年4月至2025年8月间美国私人非农企业平均时薪同比增速与CPI同比增速的走势。',
      },
      {
        type: 'page', chunk_index: 1, page_no: 1,
        text: '主线一，人工智能。作为划时代意义的科技革命……',
      },
    ]);

    expect(preferredLocation(reference)).toBe(reference.locations[0]);
  });

  it('首个带正文 location 是表格行时，按该片段的 chunk 定位', () => {
    const reference = ref('pdf', [
      { type: 'table_row', chunk_index: 7, table_idx: 1, row_start: 3, row_end: 5, text: '| 指标 | 数值 |' },
      { type: 'page', chunk_index: 1, page_no: 0, text: '无关正文' },
    ]);

    expect(preferredLocation(reference)).toBe(reference.locations[0]);
  });

  it('视频引用仍优先时间锚点', () => {
    const reference = ref('video', [
      { type: 'timestamp', chunk_index: 3, time_start: 12, time_end: 30, text: '……' },
      { type: 'page', chunk_index: 1, page_no: 0, text: '……' },
    ]);

    expect(preferredLocation(reference)).toBe(reference.locations[0]);
  });

  it('图片文档优先可直开的图片 location', () => {
    const image = {
      type: 'image' as const, chunk_index: 2, image_idx: 0, page_no: 0,
      asset_url: 'https://example.com/a.jpg', text: '图片描述',
    };
    const reference = ref('image', [image]);
    expect(preferredLocation(reference)).toBe(image);
  });
});
