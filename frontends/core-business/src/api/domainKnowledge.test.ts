/**
 * 数据源卡片的描述文案 / 路由前缀回归测试。
 *
 * 锁两件容易静默坏掉的事：
 *  1. `database` 引用行（`config_json` 由后端 list/get 回填连接的公开配置）
 *     优先显示连接名；存量内联行（没有 `connection_name`）保持原样，
 *     不能被 `· ` 前缀污染。
 *  2. 非 `database` 类型的描述与路由一字不改 —— 本次只该动 `database` 分支。
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/api/dataSource', () => ({ listDataSources: vi.fn() }));

import { listDataSources } from '@/api/dataSource';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import type { DataSourceInstance } from '@/types/dataSource';
import type { CompiledSchemaRelationType } from '@/types/domainKnowledge';

const t = (key: string) => key;

function ds(partial: Partial<DataSourceInstance>): DataSourceInstance {
  return {
    id: 'ds_1',
    knowledgeBaseId: 'kb_1',
    accessType: 'database',
    name: '资产库',
    configJson: {},
    syncMode: 'manual',
    status: 'active',
    documentCount: 0,
    ...partial,
  };
}

async function cardOf(partial: Partial<DataSourceInstance>) {
  vi.mocked(listDataSources).mockResolvedValue([ds(partial)]);
  const [card] = await getDomainKnowledgeDataSources('kb_1', t);
  return card;
}

describe('database 数据源描述', () => {
  it('引用行（config_json 由后端回填连接公开配置）把连接名摆在最前', async () => {
    const card = await cardOf({
      connectionId: 'conn_1',
      configJson: {
        // 引用行落库时是 `{}` 占位，这些字段全是后端合并进来的
        connection_name: '生产资产库',
        connection_id: 'conn_1',
        engine: 'postgresql',
        host: '10.0.0.1',
        port: 5432,
        database: 'assets_r2',
      },
    });
    expect(card.desc).toBe('生产资产库 · postgresql ｜ 10.0.0.1:5432 / assets_r2');
  });

  it('存量内联行没有 connection_name —— 描述与加连接名之前一模一样', async () => {
    const card = await cardOf({
      configJson: { engine: 'mysql', host: 'db.internal', port: 3306, database: 'legacy' },
    });
    expect(card.desc).toBe('mysql ｜ db.internal:3306 / legacy');
  });

  it('缺字段仍按 `-` 兜底，不因加了连接名而改变兜底口径', async () => {
    const card = await cardOf({ configJson: { connection_name: '只有名字' } });
    expect(card.desc).toBe('只有名字 ·  ｜ -:- / -');
  });

  it('走 schema 画布路由、用数据库图标与文案', async () => {
    const card = await cardOf({ configJson: { engine: 'postgresql' } });
    expect(card.path).toBe('/domain-knowledge/kb_1/schema/ds_1');
    expect(card.iconType).toBe('database');
    expect(card.type).toBe('domainKnowledge.dataSourceType.database');
  });
});

describe('ETL 来源不进数据源页签视图', () => {
  it('access_type=etl 的源被过滤掉，普通源原样保留', async () => {
    vi.mocked(listDataSources).mockResolvedValue([
      ds({ id: 'ds_etl', accessType: 'etl', name: '数仓每日血缘', configJson: { etl_kind: 'openlineage' } }),
      ds({ id: 'ds_file', accessType: 'file', name: '手册' }),
      ds({ id: 'ds_db', accessType: 'database', name: '资产库' }),
    ]);
    const cards = await getDomainKnowledgeDataSources('kb_1', t);
    // ETL 只活在「ETL 集成」页签；留在这里会变成「文件/手动上传」+ 死链。
    expect(cards.map((card) => card.id)).toEqual(['ds_file', 'ds_db']);
  });
});

describe('非 database 类型不受影响', () => {
  it.each([
    [
      'api',
      { endpoint: 'https://api.example.com/docs' },
      'domainKnowledge.dataSourceDesc.endpoint：https://api.example.com/docs',
      '/domain-knowledge/kb_1/datasource/sync/ds_1',
    ],
    [
      'storage',
      { backend: 'minio', bucket: 'b', prefix: 'kb/finance/' },
      'minio ｜ domainKnowledge.dataSourceDesc.bucket：b / kb/finance/',
      '/domain-knowledge/kb_1/datasource/storage/ds_1',
    ],
    [
      'api_push',
      { allowed_ext: ['pdf', 'md'] },
      'domainKnowledge.dataSourceDesc.apiPush ｜ domainKnowledge.dataSourceDesc.allowed：pdf/md',
      '/domain-knowledge/kb_1/datasource/api-push/ds_1',
    ],
  ] as const)('%s 的描述与路径保持不变', async (accessType, configJson, desc, path) => {
    const card = await cardOf({ accessType, configJson });
    expect(card.desc).toBe(desc);
    expect(card.path).toBe(path);
  });

  it('file 走 manual 路由（不带 dsId）且描述仍是「手动上传」', async () => {
    const card = await cardOf({ accessType: 'file', configJson: {} });
    expect(card.desc).toBe('domainKnowledge.dataSourceDesc.manualUpload');
    expect(card.path).toBe('/domain-knowledge/kb_1/datasource/manual');
  });
});

describe('编辑单条关系不能洗掉 join 键（P0-2）', () => {
  it('payload 不带 join 键时保留 existing 的那三个', async () => {
    const { buildCompiledRelationFromPayloadForTest } = await import('@/api/domainKnowledge');
    const existing = {
      name: 'REF_APPLYJOBS_USER', display_name: '投递用户', aliases: [],
      source: 'ApplyJobs', target: 'CompanyUser', cardinality: 'many_to_one',
      source_column: 'user_id', target_column: 'id', origin: 'declared',
    } as unknown as CompiledSchemaRelationType;

    const out = buildCompiledRelationFromPayloadForTest(
      // payload 的形状就是关系编辑弹窗交上来的那份（`sourceObject`/`targetObject` 是对象 display name，
      // 里面**没有** join 键 —— 弹窗没有这两个输入项）。
      { name: '投递用户', sourceObject: 'ApplyJobs', targetObject: 'CompanyUser', relationType: 'many_to_one' } as never,
      // 两端的 code 得能在 entityTypes 里反查到，否则函数在进到返回字面量之前就抛
      // 「Referenced object does not exist」——那样红的就不是 join 键这三条断言了。
      [{ name: 'ApplyJobs', display_name: 'ApplyJobs' }, { name: 'CompanyUser', display_name: 'CompanyUser' }] as never,
      new Set(['REF_APPLYJOBS_USER']),
      existing,
    );

    expect(out.source_column).toBe('user_id');
    expect(out.target_column).toBe('id');
    expect(out.origin).toBe('declared');
  });

  it('端点被改到第三个实体后清掉那三个键（旧端点的列名属于旧端点）', async () => {
    const { buildCompiledRelationFromPayloadForTest } = await import('@/api/domainKnowledge');
    const existing = {
      name: 'REF_APPLYJOBS_USER', display_name: '投递用户', aliases: [],
      source: 'ApplyJobs', target: 'CompanyUser', cardinality: 'many_to_one',
      source_column: 'user_id', target_column: 'id', origin: 'declared',
    } as unknown as CompiledSchemaRelationType;

    const out = buildCompiledRelationFromPayloadForTest(
      // 端点从 CompanyUser 换到 UsersChannel：`user_id` / `id` 是旧端点上的列，
      // 带到新关系上会被 ② 侧的「对端列不存在 → 回落主键」洗成一条没人声明过的等值条件。
      { name: '投递用户', sourceObject: 'ApplyJobs', targetObject: 'UsersChannel', relationType: 'many_to_one' } as never,
      [
        { name: 'ApplyJobs', display_name: 'ApplyJobs' },
        { name: 'CompanyUser', display_name: 'CompanyUser' },
        { name: 'UsersChannel', display_name: 'UsersChannel' },
      ] as never,
      new Set(['REF_APPLYJOBS_USER']),
      existing,
    );

    expect(out.source_column).toBeNull();
    expect(out.target_column).toBeNull();
    expect(out.origin).toBeNull();
  });
});
