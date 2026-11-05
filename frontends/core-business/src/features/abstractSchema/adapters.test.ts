/** v2 抽象 schema ⇄ 画布文档 的往返适配（纯函数）。
 *
 *  最贵的两条：① 往返不丢落点/背书（丢一条 = 保存时静默抹掉对齐）；
 *  ② 属性类型回落与编译画布同口径（未知类型 → string）。
 */
import { describe, expect, it } from 'vitest';

import { toDocument, toServerBody } from './adapters.js';
import type { AbstractSchemaBody } from '@/api/abstractSchema';

const BODY: AbstractSchemaBody = {
  entities: [
    {
      name: 'User', displayName: '用户', description: '账号', aliases: ['用户账号'],
      sources: [
        { dataSourceId: 'ds_a', entityName: 'Users', included: true, stale: false },
        { dataSourceId: 'ds_b', entityName: 'User', included: false, stale: false },
      ],
      attributes: [
        { name: 'id', displayName: '编号', type: 'number', primaryKey: true, required: true, sources: [
          { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'id', stale: false },
        ] },
        { name: 'memo', type: 'vector', primaryKey: false, required: false, sources: [] },
        { name: 'status', type: 'enum', primaryKey: false, required: false, enumValues: ['a'], sources: [] },
      ],
    },
    { name: 'Order', description: '', aliases: [], sources: [{ dataSourceId: 'ds_a', entityName: 'Orders', included: true, stale: false }], attributes: [] },
  ],
  relations: [
    { name: 'USER_ORDER', source: 'User', target: 'Order', cardinality: 'one_to_many', description: '',
      backings: [{ dataSourceId: 'ds_a', relationName: 'REF_UO', sourceColumn: 'user_id', targetColumn: 'id', origin: 'declared', stale: false }] },
  ],
};

describe('抽象 schema 适配', () => {
  it('往返保留落点（含 included=false）与背书', () => {
    const document = toDocument('kb_x', BODY);
    const back = toServerBody(document);

    expect(back.entities[0].sources).toEqual(BODY.entities[0].sources.map((s) => ({
      dataSourceId: s.dataSourceId, entityName: s.entityName, included: s.included, stale: s.stale,
    })));
    expect(back.relations[0].backings[0]).toMatchObject({ dataSourceId: 'ds_a', relationName: 'REF_UO', sourceColumn: 'user_id', targetColumn: 'id', origin: 'declared' });
    expect(back.entities.map((e) => e.name)).toEqual(['User', 'Order']);
  });

  it('属性类型未知回落 string；主键/必填/枚举透传', () => {
    const document = toDocument('kb_x', BODY);
    const user = document.model.entities.find((e) => e.name === 'User')!;
    expect(user.attributes[0]).toMatchObject({ type: 'number', primaryKey: true, required: true });
    expect(user.attributes[1].type).toBe('string');
    // 枚举取值是「读进来原样带出」的一格：丢了它，保存一次就把取值表抹掉
    expect(user.attributes.find((a) => a.name === 'status')!.enumValues).toEqual(['a']);
    const back = toServerBody(document);
    expect(back.entities[0].attributes[0].primaryKey).toBe(true);
    expect(back.entities[0].attributes.find((a) => a.name === 'status')!.enumValues).toEqual(['a']);
  });

  it('基数在词汇表外时不带该字段（不 throw，整页加载不因它失败）', () => {
    // 裸 cast 的老写法会让 `DomainDocumentSchema.parse` 在 toDocument 里当场 throw——
    // 上游给出 '1:N' 这类非画布词汇时，整页加载失败
    const document = toDocument('kb_x', {
      ...BODY,
      relations: [{ ...BODY.relations[0], cardinality: '1:N' }],
    });
    expect(document.model.relations[0].cardinality).toBeUndefined();
    // 词汇表内的值照旧带出
    const kept = toDocument('kb_x', { ...BODY, relations: [{ ...BODY.relations[0], cardinality: 'many_to_one' }] });
    expect(kept.model.relations[0].cardinality).toBe('many_to_one');
  });

  it('关系端点按名字解算（改实体名不扯断）', () => {
    const document = toDocument('kb_x', BODY);
    const relation = document.model.relations[0];
    const target = document.model.entities.find((e) => e.id === relation.targetEntityId)!;
    expect(target.name).toBe('Order');
  });

  it('属性 displayName 与 sources 往返不丢', () => {
    const document = toDocument('kb_x', BODY);
    const user = document.model.entities.find((e) => e.name === 'User')!;
    const drawn = user.attributes.find((a) => a.name === 'id')!;
    expect(drawn.displayName).toBe('编号');
    expect(drawn.sources).toEqual([
      { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'id', stale: false },
    ]);

    const back = toServerBody(document);
    const restored = back.entities[0].attributes.find((a) => a.name === 'id')!;
    expect(restored.displayName).toBe('编号');
    expect(restored.sources).toEqual([
      { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'id', stale: false },
    ]);
  });

  it('relations 三键与属性来源 role 往返不丢（TBox 导入线）', () => {
    const body: AbstractSchemaBody = {
      ...BODY,
      entities: [{
        ...BODY.entities[0],
        attributes: [{
          name: '地址', type: 'text', primaryKey: false, required: false,
          sources: [
            { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'city', role: '市', stale: false },
            { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'province', role: '省', stale: false },
            { dataSourceId: 'ds_a', entityName: 'Users', attributeName: 'street', stale: false },
          ],
        }],
      }, BODY.entities[1]],  // Order 必须在册：sourceTypes 的 id → 名字解算要能命中它
      relations: [{
        ...BODY.relations[0],
        sourceTypes: ['Order', 'User'], targetTypes: ['User', 'Order'],
        inverseOf: 'USER_ORDER_INV',
      }],
    };

    const document = toDocument('kb_x', body);
    const relation = document.model.relations[0];
    expect(relation.inverseOf).toBe('USER_ORDER_INV');
    expect(relation.sourceTypes).toEqual(['e:Order', 'e:User']);   // 名字 → 稳定 id
    const attr = document.model.entities[0].attributes[0];
    expect(attr.sources?.map((s) => s.role)).toEqual(['市', '省', undefined]);

    const back = toServerBody(document);
    expect(back.relations[0].inverseOf).toBe('USER_ORDER_INV');
    expect(back.relations[0].sourceTypes).toEqual(['Order', 'User']);  // id → 名字
    expect(back.entities[0].attributes[0].sources.map((s) => s.role))
      .toEqual(['市', '省', undefined]);
  });

  it('三键单值不发（单值 = 未设置，与编译画布同口径）', () => {
    const document = toDocument('kb_x', {
      ...BODY,
      relations: [{ ...BODY.relations[0], sourceTypes: ['User'] }],
    });
    const back = toServerBody(document);
    expect(back.relations[0].sourceTypes).toBeUndefined();
  });

  it('round-trips landing slots without materializing them', () => {
    const body: AbstractSchemaBody = {
      entities: [{ name: '概念', description: '', aliases: [], sources: [], attributes: [{
        name: '属性', type: 'string', primaryKey: false, required: false,
        sources: [{
          dataSourceId: 'ds', entityName: 'T', attributeName: 'c', stale: false,
          filter: 'c > 0', aggregation: 'SUM', grain: 'per day', enabled: false,
        }],
      }]}],
      relations: [],
    };
    const out = toServerBody(toDocument('kb', body));
    const src = out.entities[0]!.attributes[0]!.sources[0]!;
    expect(src.filter).toBe('c > 0');
    expect(src.aggregation).toBe('SUM');
    expect(src.grain).toBe('per day');
    expect(src.enabled).toBe(false);

    const bareBody: AbstractSchemaBody = {
      entities: [{ name: '概念', description: '', aliases: [], sources: [], attributes: [{
        name: '属性', type: 'string', primaryKey: false, required: false,
        sources: [{ dataSourceId: 'ds', entityName: 'T',
                    attributeName: 'c', stale: false }],
      }]}],
      relations: [],
    };
    const bare = toServerBody(toDocument('kb', bareBody))
      .entities[0]!.attributes[0]!.sources[0]!;
    expect('filter' in bare).toBe(false);
    expect('aggregation' in bare).toBe(false);
    expect('grain' in bare).toBe(false);
    expect('enabled' in bare).toBe(false);
  });
});
