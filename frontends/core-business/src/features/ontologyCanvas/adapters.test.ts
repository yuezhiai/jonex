/**
 * 画布保存**不能洗掉 join 键**（P0-2）。
 *
 * `toServerSchema` 只回贴白名单里的字段（`pickExtras`），而画布本身**不建模** join 键 ——
 * 所以白名单漏了哪个键，哪个键就会在保存那一刻静默消失，服务端存回去的就是没有 join 的版本。
 */
import { describe, expect, it } from 'vitest';

import { toDocument, toServerSchema } from './adapters.js';
import type { ServerConstraint, ServerEntityType, ServerSchema } from './adapters.js';
import type { Constraint, DomainDocument } from './domain/index.js';

import jinpinConstraints from './__fixtures__/jinpin-constraints-v50.json';
import kbConstraints from './__fixtures__/kb-constraints-v15.json';

const baseline = {
  entity_types: [
    { name: 'ApplyJobs', aliases: [], attributes: [{ name: 'id', type: 'number', is_primary_key: true }, { name: 'user_id', type: 'number' }] },
    { name: 'CompanyUser', aliases: [], attributes: [{ name: 'id', type: 'number', is_primary_key: true }] },
  ],
  relation_types: [
    {
      name: 'REF_APPLYJOBS_USER',
      source: 'ApplyJobs',
      target: 'CompanyUser',
      source_column: 'user_id',
      target_column: 'id',
      origin: 'derived',
    },
  ],
} as unknown as ServerSchema;

/** 最小可用文档：两个实体 + 一条关系（内容与 baseline 对齐）。
 *
 * ⚠️ `disambiguation` **不能省**：`toServerSchema` 返回值展开处读 `document.model.disambiguation`，
 * 少了它整条路在碰到任何断言之前就 `TypeError`。
 * ⚠️ `constraints` 同理不能省：写侧 `buildConstraints` 直接遍历 `document.model.constraints`
 * （真实文档经 `DomainDocumentSchema.parse` 必有此键，这里的 stub 缺了就会 `TypeError`）。
 */
const document = {
  model: {
    disambiguation: { caseInsensitive: true, aliasMerge: true },
    constraints: [],
    entities: [
      { id: 'e:ApplyJobs', name: 'ApplyJobs', aliases: [], attributes: [
        { id: 'a:ApplyJobs.id', name: 'id', type: 'number', primaryKey: true, required: true },
        { id: 'a:ApplyJobs.user_id', name: 'user_id', type: 'number', primaryKey: false, required: false },
      ] },
      { id: 'e:CompanyUser', name: 'CompanyUser', aliases: [], attributes: [
        { id: 'a:CompanyUser.id', name: 'id', type: 'number', primaryKey: true, required: true },
      ] },
    ],
    relations: [
      { id: 'r:REF_APPLYJOBS_USER', name: 'REF_APPLYJOBS_USER', sourceEntityId: 'e:ApplyJobs', targetEntityId: 'e:CompanyUser' },
    ],
  },
} as unknown as DomainDocument;

describe('toServerSchema 的 join 键保全', () => {
  it('回贴 source_column / target_column / origin', () => {
    const out = toServerSchema(document, baseline);
    const rel = (out.relation_types ?? [])[0] as unknown as Record<string, unknown>;

    expect(rel.source_column).toBe('user_id');
    expect(rel.target_column).toBe('id');
    expect(rel.origin).toBe('derived');
  });

  it('基线没有这三个键时不凭空造', () => {
    const bare = {
      entity_types: baseline.entity_types,
      relation_types: [{ name: 'REF_APPLYJOBS_USER', source: 'ApplyJobs', target: 'CompanyUser' }],
    } as unknown as ServerSchema;

    const out = toServerSchema(document, bare);
    const rel = (out.relation_types ?? [])[0] as unknown as Record<string, unknown>;

    expect('source_column' in rel).toBe(false);
    expect('target_column' in rel).toBe(false);
    expect('origin' in rel).toBe(false);
  });

  it('给端点实体改名后 join 键仍在 —— 改名不改 id，更不改物理表', () => {
    // 画布上重命名端点实体是一等操作：PropertiesPanel 的名称输入框直接改 `entity.name`，
    // **id 不变**（id 是加载时从名字派生的，见 adapters.ts 头部注释）。
    // join 键描述的是**物理表上的列**，改名不改表 —— 键依然有效，必须回贴。
    const renamed = {
      model: {
        ...document.model,
        entities: [
          document.model.entities[0],
          { ...document.model.entities[1], name: 'CompanyUserRenamed' },
        ],
      },
    } as unknown as DomainDocument;

    const out = toServerSchema(renamed, baseline);
    const rel = (out.relation_types ?? [])[0] as unknown as Record<string, unknown>;

    // 端点按**新名字**解算（改名要落到关系上），但 join 三键不能被误判成「换了端点」而清掉
    expect(rel.target).toBe('CompanyUserRenamed');
    expect(rel.source_column).toBe('user_id');
    expect(rel.target_column).toBe('id');
    expect(rel.origin).toBe('derived');
  });

  it('端点被改到别的实体后不回贴旧端点的 join 键 —— 其余 extras 照旧', () => {
    const withExtras = {
      entity_types: baseline.entity_types,
      relation_types: [{
        name: 'REF_APPLYJOBS_USER', source: 'ApplyJobs', target: 'CompanyUser',
        source_column: 'user_id', target_column: 'id', origin: 'derived',
        status: 'active', aliases: ['投递用户'], source_relation_id: 'rel_1',
      }],
    } as unknown as ServerSchema;

    // 画布上把 relation 的 target 改到了第三个实体 Dept —— 基线上的三键属于旧端点 CompanyUser
    const moved = {
      model: {
        ...document.model,
        entities: [
          ...document.model.entities,
          { id: 'e:Dept', name: 'Dept', aliases: [], attributes: [] },
        ],
        relations: [
          { ...document.model.relations[0], targetEntityId: 'e:Dept' },
        ],
      },
    } as unknown as DomainDocument;

    const out = toServerSchema(moved, withExtras);
    const rel = (out.relation_types ?? [])[0] as unknown as Record<string, unknown>;

    expect('source_column' in rel).toBe(false);
    expect('target_column' in rel).toBe(false);
    expect('origin' in rel).toBe(false);
    // 与端点无关的 extras 必须照旧回贴 —— 不能因为清了 join 键就整块丢
    expect(rel.status).toBe('active');
    expect(rel.aliases).toEqual(['投递用户']);
    expect(rel.source_relation_id).toBe('rel_1');
  });
});

// ── 约束真机夹具：实体/属性表由 target_code 程序化生成（夹具只存约束行） ──

function entityTypesFrom(constraints: ServerConstraint[]): ServerEntityType[] {
  const byEntity = new Map<string, Set<string>>();
  for (const item of constraints) {
    if (item.target_type !== 'attribute') continue;
    const code = String(item.target_code ?? '');
    const dot = code.indexOf('.');
    if (dot <= 0) continue;
    const entityName = code.slice(0, dot);
    if (!byEntity.has(entityName)) byEntity.set(entityName, new Set());
    byEntity.get(entityName)!.add(code.slice(dot + 1));
  }
  return [...byEntity.entries()].map(([name, attributes]) => ({
    name,
    aliases: [],
    attributes: [...attributes].map((attribute) => ({ name: attribute, type: 'string' })),
  }));
}

const schemaOf = (constraints: ServerConstraint[]): ServerSchema =>
  ({ entity_types: entityTypesFrom(constraints), relation_types: [], constraints } as ServerSchema);

describe('约束读侧（服务端 7 键 → 画布五档）', () => {
  it('jinpin 38 条 query_rule 全量进画布：id/目标/expression/severity 逐项对', () => {
    const document = toDocument('kb_jinpin', 'kb_jinpin', schemaOf(jinpinConstraints));

    expect(document.model.constraints).toHaveLength(38);
    const first = document.model.constraints[0] as Extract<Constraint, { type: 'query_rule' }>;
    expect(first.type).toBe('query_rule');
    expect(first.id).toBe('c:query_rule:Jobs.status');
    expect(first.entityId).toBe('e:Jobs');
    expect(first.attributeId).toBe('a:Jobs.status');
    expect(first.expression).toBe(jinpinConstraints[0]!.expression);
    expect(first.severity).toBe('error'); // 拍板①：读侧恒 error
    expect(new Set(document.model.constraints.map((item) => item.id)).size).toBe(38);
  });

  it('kb 15 条 unique_attribute 全量进画布', () => {
    const document = toDocument('kb_kbassets', 'kb_kbassets', schemaOf(kbConstraints));

    expect(document.model.constraints).toHaveLength(15);
    expect(document.model.constraints.every((item) => item.type === 'unique_attribute' && item.severity === 'error')).toBe(true);
  });

  it('demo stub（无 name/target_code）读侧跳过、不炸、不误伤其余条目', () => {
    // 真机 stub 有三种旧形状（{type:'entity',severity:'warning'} / {rule:'relation_…',type:'relation',…} /
    // {rule:'currency_unit_consistency',type:'value',fields:[…],…}，共 5 行 9 条），但都缺
    // constraint_type/target_code、走同一分支——这里单条已覆盖该分支
    const stub = { type: 'entity', severity: 'warning' } as unknown as ServerConstraint;
    const document = toDocument('kb_demo', 'kb_demo', schemaOf([stub, ...jinpinConstraints]));

    expect(document.model.constraints).toHaveLength(38);
  });

  it('未知 constraint_type（custom）与 entity 级不进画布（留给写侧保真桶）', () => {
    // 两条都引用表内**存在**的合法属性码：这样 custom 只能被白名单拒、entityLevel 只能被 target_type 拒
    // ——若拿表外码（如 T.a）就同时踩「目标不存在」，删掉白名单或 target_type 判测试照样绿，零隔离力
    const existingCode = kbConstraints[0]!.target_code;
    const custom = { name: 'custom_x', target_type: 'attribute', target_code: existingCode, constraint_type: 'custom' } as unknown as ServerConstraint;
    const entityLevel = { name: 'query_rule_T', target_type: 'entity', target_code: existingCode, constraint_type: 'query_rule' } as unknown as ServerConstraint;
    const document = toDocument('kb_x', 'kb_x', schemaOf([custom, entityLevel, ...kbConstraints]));

    expect(document.model.constraints).toHaveLength(15);
  });

  it('目标不存在的条目读侧跳过（画布模型不留悬空约束）', () => {
    const ghost = { name: 'query_rule_Ghost_a', target_type: 'attribute', target_code: 'Ghost.a', constraint_type: 'query_rule', expression: 'e' } as ServerConstraint;
    // ⚠️ 实体表只由基准夹具生成——ghost 不能混进 `entityTypesFrom`，否则它自己就会把 `Ghost`
    // 实体和 `Ghost.a` 属性造出来，「目标不存在」的前提当场不成立（照抄混入写法会红成 16 ≠ 15）
    const schema: ServerSchema = { ...schemaOf(kbConstraints), constraints: [ghost, ...kbConstraints] };
    const document = toDocument('kb_x', 'kb_x', schema);

    expect(document.model.constraints).toHaveLength(15);
  });
});

describe('约束写侧（匹配法）：round-trip 逐字 / 保真桶 / 悬空丢弃', () => {
  it('jinpin 38 条 query_rule：加载 → 不改 → 保存，全量逐字 equal', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    expect(document.model.constraints).toHaveLength(38);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(jinpinConstraints);
  });

  it('kb 15 条 unique_attribute：同上', () => {
    const baseline = schemaOf(kbConstraints);
    const document = toDocument('kb_kbassets', 'kb_kbassets', baseline);
    expect(document.model.constraints).toHaveLength(15);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(kbConstraints);
  });

  it('实体改名：target_code 跟随新名、name/target_label 沿用（目标未变）；其余条目不受影响', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    const jobs = document.model.entities.find((item) => item.id === 'e:Jobs');
    expect(jobs).toBeTruthy();
    jobs!.name = '岗位';

    const out = toServerSchema(document, baseline);
    const first = out.constraints![0]!;
    expect(first.target_code).toBe('岗位.status');
    expect(first.name).toBe('query_rule_Jobs_status');
    expect(first.target_label).toBe('status');
    // ⚠️ 夹具里凡指向 `Jobs` 的条目（共 4 条：status / iswithphone / iswearcleanclothes / fee_unit）
    // 都统一跟随改名——target_code 换新名，name/target_label 仍沿用 baseline ——
    // 所以「其余逐字不变」只对**不指向 Jobs** 的条目成立
    const expected = jinpinConstraints.map((item) =>
      item.target_code.startsWith('Jobs.')
        ? { ...item, target_code: `岗位.${item.target_code.slice('Jobs.'.length)}` }
        : item,
    );
    expect(out.constraints).toEqual(expected);
  });

  it('改目标（换到新属性）→ name 重派生、label 用新属性名', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    const jobs = document.model.entities.find((item) => item.id === 'e:Jobs')!;
    jobs.attributes.push({ id: 'a:Jobs.note', name: 'note', type: 'string', required: false, primaryKey: false });
    const first = document.model.constraints[0] as Extract<Constraint, { type: 'query_rule' }>;
    first.attributeId = 'a:Jobs.note';

    const out = toServerSchema(document, baseline);
    const rebuilt = out.constraints![0]!;
    expect(rebuilt.target_code).toBe('Jobs.note');
    expect(rebuilt.name).toBe('query_rule_Jobs_note');
    expect(rebuilt.target_label).toBe('note');
  });

  it('删除画布约束 → 该条不产出，其余逐字（画布权威）', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    document.model.constraints.splice(0, 1);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(jinpinConstraints.slice(1));
  });

  it('清空画布 → constraints: []（三态里的「明确删光」）', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    document.model.constraints = [];

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([]);
  });

  it('不可表达条目（custom）保真回贴：进不了画布、排在画布条目之后、原样 7 键', () => {
    const custom = {
      name: 'custom_Jobs_status', target_type: 'attribute', target_code: 'Jobs.status',
      target_label: 'status', constraint_type: 'custom', expression: '自由文本', suggestion: '建议',
    };
    const baseline = schemaOf([custom, ...jinpinConstraints]);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    expect(document.model.constraints).toHaveLength(38);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toHaveLength(39);
    expect(out.constraints!.slice(0, 38)).toEqual(jinpinConstraints);
    expect(out.constraints![38]).toEqual(custom);
  });

  it('保真条目跟随改名；目标被删则丢弃', () => {
    const custom = {
      name: 'custom_Jobs_status', target_type: 'attribute', target_code: 'Jobs.status',
      target_label: 'status', constraint_type: 'custom', expression: '自由文本', suggestion: '',
    };
    const baseline = schemaOf([custom]);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);

    document.model.entities.find((item) => item.id === 'e:Jobs')!.name = '岗位';
    let out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([{ ...custom, target_code: '岗位.status' }]);

    document.model.entities = [];
    out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([]);
  });

  it('畸形 stub（无 name/target_code）读写两侧都丢，不误伤其余', () => {
    const stub = { type: 'entity', severity: 'warning' } as unknown as ServerConstraint;
    const baseline = schemaOf([stub, ...jinpinConstraints]);
    const document = toDocument('kb_demo', 'kb_demo', baseline);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(jinpinConstraints);
  });

  it('悬空画布条目丢弃（不产出必被 400 的载荷）', () => {
    const baseline = schemaOf(jinpinConstraints);
    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    const first = document.model.constraints[0] as Extract<Constraint, { type: 'query_rule' }>;
    first.attributeId = 'a:Ghost.gone';

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(jinpinConstraints.slice(1));
  });

  it('value_range / relation_cardinality 模板往返（合成数据）', () => {
    const baseline = {
      entity_types: [{ name: 'User', aliases: [], attributes: [{ name: 'age', type: 'integer' }] }],
      relation_types: [{ name: 'OWNS', source: 'User', target: 'User' }],
      constraints: [
        { name: 'value_range_User_age', target_type: 'attribute', target_code: 'User.age', target_label: 'age', constraint_type: 'value_range', expression: '范围：[0, 150]', suggestion: '' },
        { name: 'relation_cardinality_OWNS', target_type: 'relation', target_code: 'OWNS', target_label: 'OWNS', constraint_type: 'relation_cardinality', expression: '基数：[0, unbounded]', suggestion: '' },
      ],
    } as unknown as ServerSchema;
    const document = toDocument('kb_x', 'kb_x', baseline);
    expect(document.model.constraints).toHaveLength(2);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(baseline.constraints);
  });

  it('required/unique 配对沿用 baseline 的 expression/suggestion（画布不建模它们）', () => {
    const baseline = {
      entity_types: [{ name: 'User', aliases: [], attributes: [{ name: 'email', type: 'string' }] }],
      relation_types: [],
      constraints: [
        { name: 'unique_attribute_User_email', target_type: 'attribute', target_code: 'User.email', target_label: 'email', constraint_type: 'unique_attribute', expression: '库内唯一', suggestion: '去重' },
      ],
    } as unknown as ServerSchema;
    const document = toDocument('kb_x', 'kb_x', baseline);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(baseline.constraints);
  });
});

describe('约束数组畸形元素（null / 非对象）防御', () => {
  it('混入 null/字符串/数字：读侧跳过、写侧丢弃、不炸、不误伤其余条目', () => {
    // 脏载荷（历史数据、手工写入）混进 constraints 数组：null 会在 `raw.constraint_type` /
    // `raw.name` 处直接 TypeError —— 读侧不炸页面、写侧不炸保存是本项规格明文。
    // 注意实体/属性表只由干净夹具生成，脏元素不能进 `entityTypesFrom`（null 在那里同样会炸）。
    const dirty = [null, 'demo-stub', 42] as unknown as ServerConstraint[];
    const baseline: ServerSchema = { ...schemaOf(jinpinConstraints), constraints: [...dirty, ...jinpinConstraints] };

    const document = toDocument('kb_jinpin', 'kb_jinpin', baseline);
    expect(document.model.constraints).toHaveLength(38);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual(jinpinConstraints);
  });
});

describe('entity 级保真桶（target_type=entity + 未知档 custom）', () => {
  // 既有覆盖只到 attribute 级未知档 custom（'不可表达条目（custom）保真回贴'）；
  // entity 级条目走同一个保真桶、但 target_type 分支与改名映射不同，单独补测。
  const entityLevel: ServerConstraint = {
    name: 'custom_Asset', target_type: 'entity', target_code: 'Asset',
    target_label: '资产', constraint_type: 'custom', expression: '自由文本', suggestion: '建议',
  };
  const baselineOf = (): ServerSchema => ({
    entity_types: [{ name: 'Asset', aliases: [], attributes: [] }],
    relation_types: [],
    constraints: [{ ...entityLevel }],
  });

  it('读：不进画布（0 条）；写：7 键原样回贴', () => {
    const baseline = baselineOf();
    const document = toDocument('kb_x', 'kb_x', baseline);
    expect(document.model.constraints).toHaveLength(0);

    const out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([entityLevel]);
  });

  it('跟随实体改名（target_code 换新名、name 沿用）；实体被删则丢弃', () => {
    const baseline = baselineOf();
    const document = toDocument('kb_x', 'kb_x', baseline);

    document.model.entities[0]!.name = '资产';
    let out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([{ ...entityLevel, target_code: '资产' }]);

    document.model.entities = [];
    out = toServerSchema(document, baseline);
    expect(out.constraints).toEqual([]);
  });
});
