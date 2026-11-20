import { describe, expect, it } from 'vitest';

import { isNoAnswerResponse } from './noAnswerDetection';

describe('isNoAnswerResponse', () => {
  it('后端返回了引用时永远不是无答案（混合答案正文提到「无法提供」也不清空）', () => {
    expect(
      isNoAnswerResponse({
        source: 'mixed',
        references: [{ doc_id: 'doc-1' }],
      }),
    ).toBe(false);
  });

  it('source=none 且没有引用 → 无答案', () => {
    expect(isNoAnswerResponse({ source: 'none', references: [] })).toBe(true);
  });

  it('source=none 但后端仍带回引用 → 保留引用', () => {
    expect(
      isNoAnswerResponse({ source: 'none', references: [{ doc_id: 'doc-1' }] }),
    ).toBe(false);
  });

  it('普通有答案的管线（ontology / rag / jonex_wiki）不需要清空引用', () => {
    expect(isNoAnswerResponse({ source: 'ontology', references: [] })).toBe(false);
    expect(isNoAnswerResponse({ source: 'rag', references: [] })).toBe(false);
    expect(isNoAnswerResponse({ source: 'jonex_wiki', references: [] })).toBe(false);
  });

  it('meta 缺失或 references 不是数组时按「非无答案」处理（宁多给引用，不误删）', () => {
    expect(isNoAnswerResponse(null)).toBe(false);
    expect(isNoAnswerResponse(undefined)).toBe(false);
    expect(isNoAnswerResponse({ source: 'mixed', references: null })).toBe(false);
  });
});
