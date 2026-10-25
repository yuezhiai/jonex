/**
 * 属性来源编辑（抽象态专供；PropertiesPanel 在 `fieldSet==='abstract'` 时渲染）。
 *
 * 2026-10-13 TBox 落点线：从只读展示升级为**可编辑**。落点 = 该抽象属性由哪些
 * 「源实体列」贡献（一对多，如「地址」← 省/市/街道/详细）；可选 `role` 是 ② 注入时
 * 的选列提示（自由文本）。**候选约束**（spec §4.1）：字段落点只能选该抽象实体
 * **已 included 落点**里的 (数据源, 源实体) 对——字段落点必须依附实体落点。
 * 变更走 `attribute.update` 命令（与显示名编辑同构），保存走页面既有 save 通路。
 */
import { useMemo, useRef, useState, type MutableRefObject, type ReactNode } from 'react';
import { Button, Checkbox, Input, Modal, Select, Tag, Tooltip, message } from 'antd';
import { BulbOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import type { Attribute, AttributeSource, DomainCommand, EntityType } from '../domain/index.js';
import type { Selection } from '../store/editorStore.js';
import type { ServerSchema } from '../adapters.js';
import { getCompiledSchema } from '../multiSource/load.js';
import { toAttributeSourceView } from './attributeSourceView.js';
import { loadSynonymIndex } from './synonymIndexCache';
import type { SynonymIndex } from './matchKernel';
import { suggestSources, type CompiledColumn, type SourceSuggestion } from './suggestSources';

interface Props {
  kbId: string;
  entity: EntityType;
  attribute: Attribute;
  execute: (command: DomainCommand, selection?: Selection) => void;
  readOnly: boolean;
  /** 编译 schema 缓存：由 PropertiesPanel 提供以便**面板内多属性复用**；
   *  未提供时回退到组件内实例缓存（单测/其他调用方场景）。 */
  schemaCache?: MutableRefObject<Map<string, ServerSchema | null>>;
  /** 同义词索引缓存（KB 级，一个 kbId 一条）：由 PropertiesPanel 提供以便**面板内多属性复用**；
   *  未提供时回退到组件内实例缓存（单测 / 其他调用方场景）。 */
  synonymCache?: MutableRefObject<Map<string, SynonymIndex>>;
}

const sameTriple = (a: AttributeSource, b: AttributeSource): boolean =>
  a.dataSourceId === b.dataSourceId && a.entityName === b.entityName
  && a.attributeName === b.attributeName;

export function AttributeSourcesBlock({ kbId, entity, attribute, execute, readOnly, schemaCache, synonymCache }: Props): ReactNode {
  const { t } = useTranslation();
  const sources = attribute.sources ?? [];
  // 候选约束（spec §4.1）：字段落点依附实体落点——只列 **included** 的落点对。
  const landedPairs = (entity.landings ?? []).filter((l) => l.included !== false);

  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickedDs, setPickedDs] = useState<string>();
  const [pickedEntity, setPickedEntity] = useState<string>();
  const [pickedColumn, setPickedColumn] = useState<string>();
  const [roleDraft, setRoleDraft] = useState('');
  const [columns, setColumns] = useState<string[]>([]);
  const [loadingColumns, setLoadingColumns] = useState(false);
  // 选实体后拉编译 schema 是异步的：连续快速换选实体时，慢响应可能后到，
  // 序号守卫丢弃过期响应，避免用旧实体的列覆盖当前实体的列清单。
  const pickSeq = useRef(0);

  const [suggestOpen, setSuggestOpen] = useState(false);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<SourceSuggestion[]>([]);
  const [pickedSuggest, setPickedSuggest] = useState<string[]>([]);
  // 取数失败与「没有候选」是两回事：失败要走错误态，不能落进 suggestEmpty 文案。
  const [suggestError, setSuggestError] = useState<string | null>(null);
  // 编译 schema 按 dsId 缓存：同一面板里多个属性点「建议」时不重复拉。
  // 面板级缓存（schemaCache prop）由 PropertiesPanel 提供、跨属性复用；未提供时回退到
  // 组件内实例缓存（单测 / 其他调用方场景）。hooks 无条件调用。
  const ownSchemaCache = useRef(new Map<string, ServerSchema | null>());
  const schemaCacheRef = schemaCache ?? ownSchemaCache;

  const ownSynonymCache = useRef(new Map<string, SynonymIndex>());
  const synonymCacheRef = synonymCache ?? ownSynonymCache;

  // 同义词表：KB 级、全局共用一份。失败**降级为空索引**（不阻塞建议），且**失败不缓存**。
  const [suggestFallback, setSuggestFallback] = useState(false);

  const suggestKey = (s: AttributeSource): string =>
    `${s.dataSourceId}\u0000${s.entityName}\u0000${s.attributeName}`;

  const openSuggest = async () => {
    setSuggestions([]); setPickedSuggest([]); setSuggestError(null); setSuggestFallback(false);
    setSuggestOpen(true);
    if (landedPairs.length === 0) return;
    setSuggestLoading(true);
    try {
      const dsIds = Array.from(new Set(landedPairs.map((l) => l.dataSourceId)));
      const [, synonymIndex] = await Promise.all([
        Promise.all(dsIds.map(async (dsId) => {
          const cacheKey = `${kbId}\u0000${dsId}`;
          if (schemaCacheRef.current.has(cacheKey)) return;
          const schema = await getCompiledSchema(kbId, dsId);
          // ⚠️ `null`（该源未编译）**不进缓存**——缓存了会把一次「暂时取不到」钉成永久的
          // 「这个源没候选」，用户重开弹窗也救不回来。
          if (schema !== null) schemaCacheRef.current.set(cacheKey, schema);
        })),
        loadSynonymIndex(synonymCacheRef.current, kbId, () => setSuggestFallback(true)),
      ]);
      const pairs = landedPairs.map((l) => {
        const schema = schemaCacheRef.current.get(`${kbId}\u0000${l.dataSourceId}`) ?? null;
        const source = (schema?.entity_types ?? []).find((e) => e.name === l.entityName);
        const columns: CompiledColumn[] = (source?.attributes ?? []).map((a) => ({
          name: String(a.name),
          ...(a.display_name ? { displayName: String(a.display_name) } : {}),
        }));
        return { dataSourceId: l.dataSourceId, entityName: l.entityName, columns };
      });
      setSuggestions(suggestSources(attribute, pairs, synonymIndex));
    } catch (e) {
      setSuggestError(e instanceof Error ? e.message : String(e));
    } finally {
      setSuggestLoading(false);
    }
  };

  const suggestGroups = useMemo(() => {
    const map = new Map<string, SourceSuggestion[]>();
    for (const item of suggestions) {
      const key = `${item.dataSourceId}\u0000${item.entityName}`;
      map.set(key, [...(map.get(key) ?? []), item]);
    }
    return [...map.entries()];
  }, [suggestions]);

  const confirmSuggest = () => {
    const chosen = suggestions.filter((s) => pickedSuggest.includes(suggestKey(s)));
    if (chosen.length === 0) return;
    const added: AttributeSource[] = chosen.map((s) => ({
      dataSourceId: s.dataSourceId, entityName: s.entityName, attributeName: s.attributeName,
    }));
    updateSources([...sources, ...added], { kind: 'entity', id: entity.id });
    setSuggestOpen(false);
  };

  const updateSources = (next: AttributeSource[], selection?: Selection) =>
    execute({ type: 'attribute.update', entityId: entity.id, attributeId: attribute.id,
              changes: { sources: next } }, selection);

  const removeSource = (source: AttributeSource) =>
    updateSources(sources.filter((i) => !sameTriple(i, source)),
                  { kind: 'entity', id: entity.id });

  // 必须**展开原对象**（`{...i, role}`），不能只挑四个基础键重建：落点还带 P1-5
  // 语义槽（filter/aggregation/grain/enabled），重建会把它们静默丢掉。
  const changeRole = (source: AttributeSource, role: string) =>
    updateSources(sources.map((i) => (sameTriple(i, source)
      ? { ...i, role: role.trim() ? role.trim() : undefined } : i)));

  const sourceOptions = Array.from(
    new Map(landedPairs.map((l) => [l.dataSourceId, l])).values(),
  ).map((l) => ({ value: l.dataSourceId, label: l.sourceName || l.dataSourceId }));

  const entityOptions = landedPairs
    .filter((l) => l.dataSourceId === pickedDs)
    .map((l) => ({ value: l.entityName, label: l.entityName }));

  const openPicker = () => {
    setPickedDs(undefined); setPickedEntity(undefined); setPickedColumn(undefined);
    setRoleDraft(''); setColumns([]); setPickerOpen(true);
  };

  const onPickEntity = async (dsId: string, entityName: string) => {
    const seq = ++pickSeq.current;
    setPickedEntity(entityName);
    setPickedColumn(undefined);
    setLoadingColumns(true);
    setColumns([]);
    try {
      const schema = await getCompiledSchema(kbId, dsId);
      if (seq !== pickSeq.current) return;
      const source = (schema?.entity_types ?? []).find((e) => e.name === entityName);
      setColumns((source?.attributes ?? []).map((a) => String(a.name)));
    } catch (e) {
      if (seq !== pickSeq.current) return;
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === pickSeq.current) setLoadingColumns(false);
    }
  };

  const duplicated = Boolean(pickedDs && pickedEntity && pickedColumn
    && sources.some((s) => s.dataSourceId === pickedDs && s.entityName === pickedEntity
        && s.attributeName === pickedColumn));

  const confirmAdd = () => {
    if (!pickedDs || !pickedEntity || !pickedColumn || duplicated) return;
    const next: AttributeSource = {
      dataSourceId: pickedDs, entityName: pickedEntity, attributeName: pickedColumn,
      ...(roleDraft.trim() ? { role: roleDraft.trim() } : {}),
    };
    updateSources([...sources, next], { kind: 'entity', id: entity.id });
    setPickerOpen(false);
  };

  // 只读 + 空：维持升级前行为（不渲染）；只读 + 有来源：只读展示（role 输入框禁用）。
  if (readOnly && !sources.length) return null;

  return (
    <div className="ae-attr-sources">
      <div className="ae-attr-sources-head">
        <span>{t('attribute.sources')}</span>
        <Button size="small" type="link" icon={<BulbOutlined />} disabled={readOnly}
          onClick={() => void openSuggest()}>{t('attributeSources.suggest')}</Button>
        <Button size="small" type="link" icon={<PlusOutlined />} disabled={readOnly}
          onClick={openPicker}>{t('attributeSources.add')}</Button>
      </div>
      {/* 来源块的产生方式提示：这些 (源, 实体, 列) 落点通常由生成/对齐自动带出，不是用户逐条手工登记。 */}
      {sources.length ? (
        <p className="ae-sources-hint">{t('attributeSources.originHint')}</p>
      ) : null}
      {sources.length === 0 ? (
        <p className="ae-landings-empty">{t('attributeSources.empty')}</p>
      ) : sources.map((source) => {
        const view = toAttributeSourceView(source, entity.landings ?? []);
        const label = view.sourceName
          ?? (view.sourceGone ? t('abstractSchema.staleSourceGone') : view.dataSourceId);
        return (
          <div className="ae-attr-source-row"
            key={`${source.dataSourceId}:${source.entityName}:${source.attributeName}`}>
            <Tag className="ae-landing-src">{label}</Tag>
            <code>{view.column}</code>
            <Input
              className="ae-attr-source-role"
              size="small"
              disabled={readOnly}
              value={source.role ?? ''}
              placeholder={t('attributeSources.rolePlaceholder')}
              onChange={(event) => changeRole(source, event.target.value)}
            />
            {view.stale ? (
              <Tooltip title={t(`abstractEditor.stale.${view.staleReason ?? 'attribute_gone'}`)}>
                <Tag color="warning">{t('abstractEditor.staleTag')}</Tag>
              </Tooltip>
            ) : null}
            <Button size="small" type="text" danger disabled={readOnly}
              onClick={() => removeSource(source)}>{t('common.delete')}</Button>
          </div>
        );
      })}

      <Modal
        open={pickerOpen}
        title={t('attributeSources.pickerTitle')}
        footer={null}
        onCancel={() => setPickerOpen(false)}
        destroyOnHidden
      >
        {landedPairs.length === 0 ? (
          <p className="ae-landings-empty">{t('attributeSources.noLanding')}</p>
        ) : (
          <>
            <Select
              style={{ width: '100%', marginBottom: 8 }}
              placeholder={t('attributeSources.pickSource')}
              value={pickedDs}
              onChange={(v) => { setPickedDs(v); setPickedEntity(undefined);
                                 setPickedColumn(undefined); setColumns([]); }}
              options={sourceOptions}
            />
            {pickedDs ? (
              <Select
                style={{ width: '100%', marginBottom: 8 }}
                placeholder={t('attributeSources.pickEntity')}
                value={pickedEntity}
                onChange={(v) => void onPickEntity(pickedDs, v)}
                options={entityOptions}
              />
            ) : null}
            {pickedEntity ? (
              loadingColumns ? (
                // 列清单加载中**保持 Select 挂载**（`loading` 转圈），不换 `<p>`、也不 `disabled`：
                // 一是不跳版（避免「选完实体后第三格短暂消失」的闪烁），二是既有测试在加载期
                // 就对这一格 mouseDown——`disabled` 会让下拉打不开，随后的选项断言落空
                // （Task 5「Select 恒挂载」裁决的延续）。
                <Select style={{ width: '100%', marginBottom: 8 }} loading
                  placeholder={t('attributeSources.pickColumn')} />
              ) : columns.length === 0 ? (
                <p className="ae-landings-empty">{t('attributeSources.noColumn')}</p>
              ) : (
                <Select
                  style={{ width: '100%', marginBottom: 8 }}
                  placeholder={t('attributeSources.pickColumn')}
                  value={pickedColumn}
                  onChange={setPickedColumn}
                  options={columns.map((c) => ({ value: c, label: c }))}
                />
              )
            ) : null}
            <Input
              style={{ marginBottom: 8 }}
              placeholder={t('attributeSources.rolePlaceholder')}
              value={roleDraft}
              onChange={(e) => setRoleDraft(e.target.value)}
            />
            {duplicated ? (
              <p className="ae-landings-empty">{t('attributeSources.duplicated')}</p>
            ) : null}
            <Button type="primary" block disabled={!pickedColumn || duplicated}
              onClick={confirmAdd}>{t('attributeSources.confirm')}</Button>
          </>
        )}
      </Modal>

      <Modal
        open={suggestOpen}
        title={t('attributeSources.suggestTitle')}
        footer={null}
        onCancel={() => setSuggestOpen(false)}
        destroyOnHidden
      >
        {/* 降级提示放弹窗顶部、三种状态之外：同义词表挂了又没有名字命中时，
            只显示「没有候选」会让用户以为真的没有，而不知道桥没接上。 */}
        {suggestFallback ? (
          <p className="ae-landings-empty">{t('attributeSources.suggestFallback')}</p>
        ) : null}
        {landedPairs.length === 0 ? (
          <p className="ae-landings-empty">{t('attributeSources.noLanding')}</p>
        ) : suggestLoading ? (
          <p className="ae-landings-empty">{t('attributeSources.suggestLoading')}</p>
        ) : suggestError !== null ? (
          <>
            <p className="ae-landings-empty" role="alert">
              {`${t('attributeSources.suggestError')}：${suggestError}`}
            </p>
            <Button block onClick={() => void openSuggest()}>
              {t('attributeSources.suggestRetry')}
            </Button>
          </>
        ) : suggestions.length === 0 ? (
          <p className="ae-landings-empty">{t('attributeSources.suggestEmpty')}</p>
        ) : (
          <>
            {suggestGroups.map(([key, items]) => {
              const first = items[0]!;
              const landing = landedPairs.find(
                (l) => l.dataSourceId === first.dataSourceId && l.entityName === first.entityName);
              return (
                <div className="ae-suggest-group" key={key}>
                  <div className="ae-suggest-group-head">
                    {`${landing?.sourceName || first.dataSourceId} · ${first.entityName}`}
                  </div>
                  {items.map((item) => {
                    const k = suggestKey(item);
                    return (
                      <Checkbox
                        key={k}
                        checked={pickedSuggest.includes(k)}
                        onChange={(event) => setPickedSuggest((prev) => (
                          event.target.checked ? [...prev, k] : prev.filter((x) => x !== k)))}
                      >
                        <code>{item.attributeName}</code>
                        {item.displayName ? (
                          <span className="ae-suggest-display">{item.displayName}</span>
                        ) : null}
                        <Tag className="ae-suggest-hit">{t(`attributeSources.hit${item.hit[0]!.toUpperCase()}${item.hit.slice(1)}`)}</Tag>
                      </Checkbox>
                    );
                  })}
                </div>
              );
            })}
            <Button type="primary" block disabled={pickedSuggest.length === 0}
              onClick={confirmSuggest}>
              {t('attributeSources.suggestAdd', { count: pickedSuggest.length })}
            </Button>
          </>
        )}
      </Modal>
    </div>
  );
}
