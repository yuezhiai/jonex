/** 第五档 query_rule 必须过 DomainDocumentSchema.parse——不落档的话画布加载直接炸（discriminatedUnion 拒收）。 */
import { describe, expect, it } from 'vitest';

import { DomainDocumentSchema } from './schemas.js';

const base = {
  storageVersion: 1,
  schemaVersion: 1,
  revision: 0,
  domain: { id: 'kb_1', displayName: 'kb_1' },
  model: {
    entities: [],
    relations: [],
    disambiguation: { caseInsensitive: true, aliasMerge: true },
  },
  editor: { nodePositions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
  savedAt: '2026-10-07T00:00:00Z',
};

describe('ConstraintSchema 第五档 query_rule', () => {
  it('query_rule 通过 parse 且 expression 保留', () => {
    const doc = {
      ...base,
      model: {
        ...base.model,
        constraints: [
          {
            id: 'c:query_rule:Jobs.status',
            type: 'query_rule',
            entityId: 'e:Jobs',
            attributeId: 'a:Jobs.status',
            severity: 'error',
            expression: '「在招」= status=1',
          },
        ],
      },
    };

    const parsed = DomainDocumentSchema.parse(doc);
    expect(parsed.model.constraints).toHaveLength(1);
    expect(parsed.model.constraints[0]).toMatchObject({
      type: 'query_rule',
      entityId: 'e:Jobs',
      attributeId: 'a:Jobs.status',
      expression: '「在招」= status=1',
    });
  });
});

describe('DomainDocumentSchema domain.id', () => {
  it('允许 UUID 风格的首字符数字（知识库 id 常见）', () => {
    const doc = {
      ...base,
      domain: { id: '1e2797b4e0f54c99b2ea9ced3ecb61b4', displayName: '测试demo' },
      model: { ...base.model, constraints: [] },
    };
    const parsed = DomainDocumentSchema.parse(doc);
    expect(parsed.domain.id).toBe('1e2797b4e0f54c99b2ea9ced3ecb61b4');
  });
});
