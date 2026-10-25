/**
 * 本体图形编辑页：`/domain-knowledge/:kbId/schema/:dsId`
 *
 * 一个**数据源**一份 schema，用画布编辑。粒度与 Jonex 的
 * `ontology_compiled_schemas.data_source_id` 对齐；知识库级（`dsId='-'`）自 v2 起
 * 由抽象编辑器接管，不再直编遗留行（见文件末尾的 `OntologyCanvasRoute`）。
 *
 * 移植自 nexonto 的 `ModelPage`，但**不含**：文档抽取、AI 操作、模板导入导出、
 * YAML 导入导出（那些在服务端有自己的入口）。这里只做一件事——把 schema 画出来、
 * 让人改、存回去。
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Alert, Button, Empty, Input, Space, Spin, Tag, Tooltip, message } from 'antd';
import { ArrowLeftOutlined, PartitionOutlined, PlusOutlined, RedoOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';

import { getData, putData, request, type ApiResponse } from '@/api/request';

import { AbstractSchemaEditorPage } from '../abstractSchema/AbstractSchemaEditorPage.js';

import { toDocument, toServerSchema, type ServerSchema } from './adapters.js';
import { ModelCanvas } from './components/ModelCanvas.js';
import { PropertiesPanel } from './components/PropertiesPanel.js';
import { checkForExport } from './domain/index.js';
import { layoutAll } from './layout.js';
import { useEditorStore } from './store/editorStore.js';
import './styles.scss';

/** `-` 作为「知识库级」的占位——路由参数不能为空，而 data_source_id 为 NULL 是有意义的取值 */
const KB_LEVEL = '-';

interface Props {
  kbId: string;
  dsId: string;
}

/**
 * 画布这一轮实际读到的 schema 落在哪个槽位。
 *
 * **读、写、乐观锁版本必须同进同退**——否则就是这台项目踩过的老坑：
 * 「画布上改得挺好，检索永远看不见」。捆成一个对象，就不会出现
 * 「版本取自这一份、写回那一份」的错配。
 */
interface SchemaSlot {
  /** 写回用哪个槽位。`undefined` = 知识库级（保存时**不带** `data_source_id`） */
  dataSourceId?: string;
  /** 该行的 `schema_version`（乐观锁是**逐份**比对的）；行不存在时为 0 */
  version: number;
  /** 本要读数据源级、但那份为空，本轮读的是知识库级 */
  fellBack: boolean;
  /** 回落读到的那一份是**抽象投影**（`source_type="abstract"`）——不是可编辑的真实行，
   *  保存已停用（后端防洗行护栏同样拒，见 ontology_compiler.save_compiled_schema） */
  abstract?: boolean;
  /** 加载时的服务端原文，保存时用来回贴画布不建模的字段（见 adapters.toServerSchema） */
  baseline?: ServerSchema;
}

/**
 * 「这一份没有可用内容」。
 *
 * 只判 `payload == null` 不够——空壳行（`entity_types: []`）会让画布继续白着，
 * 而白着的画布一按保存就会**新建**一份空的数据源级行，把状态永久钉死。
 * 也不能只看 `entity_types`——空壳行的 `relation_types` 同样是空的。
 */
function isEmptySchema(payload: ServerSchema | null | undefined): boolean {
  if (!payload) return true;
  return !payload.entity_types?.length && !payload.relation_types?.length;
}

export function OntologyCanvasPage({ kbId, dsId }: Props): ReactNode {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  /** 读、写、锁三者共用的「这一份」——取代原来的裸 `schemaVersion` */
  const [schemaSlot, setSchemaSlot] = useState<SchemaSlot>({ version: 0, fellBack: false });
  /** 递增即请求画布重新取景。视口状态在 CanvasInner 里（`useReactFlow` 必须在 Provider 内），
   *  跨不过去，所以用信号而不是回调。 */
  const [relayoutTick, setRelayoutTick] = useState(0);

  const store = useEditorStore();
  const { workingDocument: document } = store;

  const dataSourceId = dsId === KB_LEVEL ? undefined : dsId;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    // 换了 kb/数据源先把槽位归零——加载完成前 document 也是空的、保存按钮出不来，
    // 但归零能让任何提前 return 都不留下上一份的版本号
    setSchemaSlot({ dataSourceId, version: 0, fellBack: false });
    store.reset();
    (async () => {
      try {
        // request 是裸 axios 且服务端还包了一层 {success,data} 信封——
        // getData 同时解掉这两层（putData 同理，所以保存那条不用再包）。
        // 服务端「没有这一份」时给的是 data:null，不是空对象。
        const readSchema = (sourceId?: string) =>
          getData<ServerSchema | null>(
            request.get<ApiResponse<ServerSchema | null>>('/knowledge-base/ontology/compiled-schema', {
              params: { knowledge_base_id: kbId, ...(sourceId ? { data_source_id: sourceId } : {}) },
            }),
          );

        let payload = await readSchema(dataSourceId);
        if (cancelled) return;
        let slot: SchemaSlot = {
          dataSourceId, version: payload?.schema_version ?? 0, fellBack: false, baseline: payload ?? undefined,
        };

        // 数据源级这份为空（没编译过，或只是个空壳）→ 回落到知识库级那份。
        //
        // 服务端**故意**不做这个回落（会把 KB 级那份错当成数据源的，见 ontology_compiler
        // 的注释），所以只能在画布这一层做。关键：`slot.dataSourceId` 保持 undefined，
        // 保存就会写回 `data_source_id IS NULL` 那一行——**也就是检索实际读的那一行**。
        // 现状是全库 data_source_id 皆 NULL，所以这是常态路径，不是异常路径。
        if (dataSourceId && isEmptySchema(payload)) {
          payload = await readSchema(undefined);
          if (cancelled) return;
          slot = {
            version: payload?.schema_version ?? 0,
            fellBack: true,
            baseline: payload ?? undefined,
            // 读到抽象投影（`source_type="abstract"`）时这份**没有**可写回去的真实行：
            // 保存会以 KB 级写回、被投影继续遮蔽（后端防洗行护栏 1003 同样会拒）。
            // 标记出来 → 禁保存 + 把原因摆在页面上。
            abstract: payload?.source_type === 'abstract',
          };
        }

        const doc = toDocument(kbId, kbId, payload ?? {});
        // 自动布局：本移植不持久化节点坐标，每次加载按关系图重算
        doc.editor.nodePositions = layoutAll(doc);
        setSchemaSlot(slot);
        store.initialize(doc);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // store 是稳定引用（zustand），不进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId, dataSourceId]);

  const issues = useMemo(() => (document ? checkForExport(document) : []), [document]);
  const mustFix = issues.filter((item) => item.level === 'mustFix');
  const confirms = issues.filter((item) => item.level === 'confirm');

  const save = useCallback(async () => {
    if (!document) return;
    setSaving(true);
    try {
      const payload = toServerSchema(document, schemaSlot.baseline);
      // ⚠ PUT 返回的是**整行 schema**，不是 `{revision, savedAt}`——原代码的泛型标注是错的，
      // 于是 `body.revision` 恒为 undefined：`markSaved` 记了个 undefined 的版本，
      // 第二次保存就会以 `expected_schema_version: undefined` 打过去，
      // 被服务端判成「Missing expected_schema_version」400。
      // 画布一直空着、没人连按两次保存，所以这个坑一直没露出来。
      const body = await putData<{ schema_version?: number; edited_at?: string }>(
        '/knowledge-base/ontology/compiled-schema',
        {
          knowledge_base_id: kbId,
          // 写回**读到的那一份**：回落读的是知识库级，这里就不带 data_source_id。
          // 绝不能改成 dataSourceId——那会写进一份空的、检索看不见的数据源级行。
          ...(schemaSlot.dataSourceId ? { data_source_id: schemaSlot.dataSourceId } : {}),
          entity_types: payload.entity_types,
          relation_types: payload.relation_types,
          // 约束恒发（非空或 `[]`）：`[]` = 显式空集合（删光/本就为空）。写侧负责保真
          // （不可表达条目原样回贴、改名跟随、悬空丢弃），见 adapters.toServerSchema 的尾注。
          constraints: payload.constraints,
          // 乐观锁是**逐份**的，版本必须和上面那个槽位同源
          expected_schema_version: schemaSlot.version,
        },
      );
      // 该行的版本已自增（新建行时服务端给 1），下一次保存拿它当 expected
      const revision = body.schema_version ?? schemaSlot.version;
      store.markSaved(revision, body.edited_at ?? '');
      setSchemaSlot((current) => ({ ...current, version: revision }));
      message.success(t('ontologyCanvas.saved'));
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      // 乐观锁冲突是可预期的并发情形，单独给一句能照做的提示。
      // 服务端那句话里带的是「已被更新」（CompileTab 用的是同一个判据）
      if (text.includes('CONFLICT') || text.includes('版本') || text.includes('已被更新')) {
        message.error(t('ontologyCanvas.conflict'));
      } else {
        message.error(text);
      }
    } finally {
      setSaving(false);
    }
  }, [document, kbId, schemaSlot, store, t]);

  const addEntity = useCallback(() => {
    if (!document) return;
    const used = new Set(document.model.entities.map((item) => item.name));
    let index = document.model.entities.length + 1;
    while (used.has(`Entity_${index}`)) index += 1;
    const name = `Entity_${index}`;
    // id 与 adapters 的派生规则保持一致（`e:` 前缀），保存时按名字回填
    store.execute(
      { type: 'entity.add', entity: { id: `e:${name}`, name, aliases: [], attributes: [] }, position: { x: 80, y: 80 } },
      { kind: 'entity', id: `e:${name}` },
    );
  }, [document, store]);

  const addAttribute = useCallback((entityId: string, type: string) => {
    if (!document) return;
    const entity = document.model.entities.find((item) => item.id === entityId);
    if (!entity) return;
    const used = new Set(entity.attributes.map((item) => item.name));
    let index = entity.attributes.length + 1;
    while (used.has(`attr_${index}`)) index += 1;
    const name = `attr_${index}`;
    store.execute(
      { type: 'attribute.add', entityId, attribute: { id: `a:${entity.name}.${name}`, name, type: type as never, required: false, primaryKey: false } },
      { kind: 'entity', id: entityId },
    );
  }, [document, store]);

  /** 重新布局：按连接度重排全部节点。
   *
   *  为什么还需要这个按钮——首次加载会自动布局，但节点被拖动后坐标就散了
   *  （`node.move` 会把坐标写进文档），想回到整齐的网格只能手动一个个拖。
   *
   *  坐标变更走 `layout.apply` 命令，所以**可撤销**（与 `node.move` 同一套历史）。
   *  第二参不传 = 保留当前选中（见 `editorStore.execute`）。 */
  const relayout = useCallback(() => {
    if (!document) return;
    store.execute({ type: 'layout.apply', positions: layoutAll(document) });
    setRelayoutTick((tick) => tick + 1);
  }, [document, store]);

  if (loading) return <div className="ontology-canvas-loading"><Spin /></div>;
  if (error) return <Alert type="error" showIcon message={t('ontologyCanvas.loadFailed')} description={error} />;
  if (!document) return <Empty description={t('ontologyCanvas.empty')} />;

  return (
    <div className="ontology-canvas-page">
      <header className="ontology-canvas-toolbar">
        <Space>
          {/* 从「数据源 → 编辑 schema」进来是一条没有出口的路：加返回，回该库的设置页 */}
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(`/domain-knowledge/${kbId}/detail`)}
          >
            {t('common.back')}
          </Button>
          <strong>{document.domain.displayName || document.domain.id}</strong>
          {/* 层级按**实际读到的那一份**标，不按 URL——回落时标「数据源级」就是撒谎。
              读到抽象投影时同理：它不是知识库级那一行，标「知识库级」也是撒谎 */}
          <Tag color={schemaSlot.abstract ? 'purple' : undefined}>
            {schemaSlot.abstract
              ? t('ontologyCanvas.abstractManaged')
              : schemaSlot.dataSourceId ? t('ontologyCanvas.dataSourceLevel') : t('ontologyCanvas.kbLevel')}
          </Tag>
          {schemaSlot.fellBack && !schemaSlot.abstract ? (
            <Tooltip title={t('ontologyCanvas.fellBackHint')}>
              <Tag color="blue">{t('ontologyCanvas.fellBack')}</Tag>
            </Tooltip>
          ) : null}
          {document.model.entities.length ? <Tag>{t('ontologyCanvas.counts', { entities: document.model.entities.length, relations: document.model.relations.length })}</Tag> : null}
          {store.dirty ? <Tag color="orange">{t('ontologyCanvas.dirty')}</Tag> : null}
        </Space>
        <Space>
          <Button icon={<UndoOutlined />} disabled={!store.past.length} onClick={store.undo} />
          <Button icon={<RedoOutlined />} disabled={!store.future.length} onClick={store.redo} />
          <Tooltip title={t('ontologyCanvas.relayoutHint')}>
            <Button icon={<PartitionOutlined />} disabled={!document.model.entities.length} onClick={relayout}>
              {t('ontologyCanvas.relayout')}
            </Button>
          </Tooltip>
          <Button icon={<PlusOutlined />} onClick={addEntity}>{t('ontologyCanvas.addEntity')}</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save} disabled={schemaSlot.abstract}>{t('common.save')}</Button>
        </Space>
      </header>

      {/* 打开的是数据源、改的却是知识库级那一份（所有数据源共用）——必须说出来。
          但只占**一行**：整块 Alert（message + description）会吃掉画布高度，
          完整解释留在工具栏那个 Tag 的 tooltip 里。
          抽象投影这一档更重：这份内容**没有**可写回去的真实行，保存已停用。 */}
      {schemaSlot.abstract ? (
        <div className="ontology-canvas-notice ontology-canvas-notice--blocked" title={t('ontologyCanvas.abstractManagedNotice')}>
          {t('ontologyCanvas.abstractManagedNotice')}
        </div>
      ) : schemaSlot.fellBack ? (
        <div className="ontology-canvas-notice" title={t('ontologyCanvas.fellBackHint')}>
          {t('ontologyCanvas.fellBackNotice')}
        </div>
      ) : null}

      {/* 校验结果走同一套 checks：mustFix 拦住保存的语义，confirm 只提示 */}
      {mustFix.length ? (
        <Alert
          type="error" showIcon className="ontology-canvas-issues"
          message={t('ontologyCanvas.mustFix', { n: mustFix.length })}
          description={mustFix.slice(0, 5).map((item) => `${item.code} · ${item.target?.id ?? '-'}`).join('；')}
        />
      ) : confirms.length ? (
        <Alert
          type="warning" showIcon className="ontology-canvas-issues"
          message={t('ontologyCanvas.confirm', { n: confirms.length })}
        />
      ) : null}

      <div className="ontology-canvas-body">
        <aside className="ontology-canvas-list">
          {document.model.entities.length === 0 ? (
            <p className="ontology-canvas-list-empty">{t('editor.emptyCanvas')}</p>
          ) : document.model.entities.map((entity) => {
            const selected = store.selection?.kind === 'entity' && store.selection.id === entity.id;
            return (
              <button
                key={entity.id}
                type="button"
                className={`ontology-canvas-list-item ${selected ? 'is-selected' : ''}`}
                onClick={() => store.select({ kind: 'entity', id: entity.id })}
              >
                <span className="name">{entity.displayName || entity.name}</span>
                <span className="code">{entity.name}</span>
                <span className="count">{entity.attributes.length}</span>
              </button>
            );
          })}
        </aside>

        <ModelCanvas
          document={document}
          selection={store.selection}
          execute={store.execute}
          select={store.select}
          addEntity={addEntity}
          addAttribute={addAttribute}
          relayoutTick={relayoutTick}
        />

        <PropertiesPanel
          document={document}
          selection={store.selection}
          execute={store.execute}
          addAttribute={addAttribute}
          select={store.select}
        />
      </div>
    </div>
  );
}

/** 路由包装：从 URL 取 kbId / dsId。`dsId='-'`（知识库级）已由抽象编辑器接管——
 *  旧入口直编遗留 compiled 行，v2 之后那是投影的目标行、不许直写，一律转到编辑器。
 *  （路由级直开没有 `detail.can_write` 上下文，`canWrite` 按 `true`，写权限由后端兜底。） */
export function OntologyCanvasRoute(): ReactNode {
  const { kbId = '', dsId = KB_LEVEL } = useParams<{ kbId: string; dsId: string }>();
  if (dsId === KB_LEVEL) {
    return <AbstractSchemaEditorPage kbId={kbId} canWrite />;
  }
  return <OntologyCanvasPage kbId={kbId} dsId={dsId} />;
}

export default OntologyCanvasRoute;
