/**
 * 实体落点面板（抽象编辑器专用，注入 PropertiesPanel 的 entityExtras 插槽）。
 *
 * 落点 = 该抽象实体在各数据源里的实体（对齐事实）。`included` 勾选控制
 * 「是否纳入 schema」——不勾的灰显保留，防重新生成时又得从头找。
 * 「从源添加实体」= 单落点新实体（未参与对齐的源实体拉进来）。
 *
 * 行内「打开源画布」跳数据源级画布（`/domain-knowledge/{kbId}/schema/{dsId}`）——查看类入口，
 * active 只读下照常可用。
 */
import { useRef, useState, type ReactNode } from 'react';
import { Button, Checkbox, Modal, Select, Tag, Tooltip, message } from 'antd';
import { BulbOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import type { DomainCommand, EntityType } from '../ontologyCanvas/domain/index.js';
import type { Selection } from '../ontologyCanvas/store/editorStore.js';
import type { ServerSchema } from '../ontologyCanvas/adapters.js';
import { getCompiledSchema } from '../ontologyCanvas/multiSource/load.js';
import { HIT_ORDER, type HitKind, type SynonymIndex } from '../ontologyCanvas/components/matchKernel';
import { loadSynonymIndex } from '../ontologyCanvas/components/synonymIndexCache';
import {
  suggestEntityLandings, type CandidateEntity, type EntitySuggestion, type EntitySuggestionGroup,
} from '../ontologyCanvas/components/suggestEntityLandings';
import { canOpenSourceCanvas, sourceCanvasPath } from './landings.js';
import { listDataSources } from '@/api/dataSource';
import { compiledSchemaSources } from '@/utils/dataSourceDisplay';

/** 命中档 → i18n key（`abstractEditor.hitExact` / `hitDisplay` / …）。 */
const hitLabelKey = (hit: HitKind): string =>
  `abstractEditor.hit${hit[0]!.toUpperCase()}${hit.slice(1)}`;

interface Props {
  kbId: string;
  entity: EntityType;
  /** 全部实体名（新落点不许撞已有实体名 → 由调用方给出） */
  entityNames: string[];
  execute: (command: DomainCommand, selection?: Selection) => void;
  readOnly: boolean;
}

interface SourceOption { id: string; name: string; }

export function LandingSection({ kbId, entity, entityNames, execute, readOnly }: Props): ReactNode {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [sources, setSources] = useState<SourceOption[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickedSource, setPickedSource] = useState<string>();
  const [sourceEntities, setSourceEntities] = useState<string[]>([]);
  const [loadingEntities, setLoadingEntities] = useState(false);

  const [suggestOpen, setSuggestOpen] = useState(false);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [suggestFallback, setSuggestFallback] = useState(false);
  const [suggestPartial, setSuggestPartial] = useState(0);
  const [suggestGroups, setSuggestGroups] = useState<EntitySuggestionGroup[]>([]);
  const [weakExpanded, setWeakExpanded] = useState<Set<string>>(new Set());
  const [pickedSuggest, setPickedSuggest] = useState<string[]>([]);
  // 编译 schema 与同义词索引的面板级缓存（同一面板内多次点「建议落点」不重复拉）
  const schemaCacheRef = useRef(new Map<string, ServerSchema | null>());
  const synonymCacheRef = useRef(new Map<string, SynonymIndex>());

  const landings = entity.landings ?? [];

  const ensureSources = async () => {
    if (sources.length) return;
    try {
      // ETL 来源没有自己的编译 schema，不能当落点目标；其余类型原样保留。
      const list = compiledSchemaSources(await listDataSources(kbId));
      setSources(list.map((ds) => ({ id: ds.id, name: ds.name })));
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  const updateLandings = (next: EntityType['landings']) => {
    execute({ type: 'entity.update', entityId: entity.id, changes: { landings: next } });
  };

  const suggestionKey = (s: EntitySuggestion): string => `${s.dataSourceId}\u0000${s.entityName}`;

  const openSuggestLandings = async () => {
    setSuggestOpen(true); setSuggestError(null); setSuggestFallback(false);
    setSuggestPartial(0); setSuggestGroups([]); setPickedSuggest([]); setWeakExpanded(new Set());
    setSuggestLoading(true);
    try {
      const [allSources, synonymIndex] = await Promise.all([
        listDataSources(kbId),
        loadSynonymIndex(synonymCacheRef.current, kbId, () => setSuggestFallback(true)),
      ]);
      // 同 ensureSources：ETL 来源没有自己的编译 schema，不参与建议落点。
      const dsList = compiledSchemaSources(allSources);
      setSources(dsList.map((ds) => ({ id: ds.id, name: ds.name })));

      const candidates: CandidateEntity[] = [];
      let failed = 0;
      await Promise.all(dsList.map(async (ds) => {
        const cacheKey = `${kbId}\u0000${ds.id}`;
        try {
          if (!schemaCacheRef.current.has(cacheKey)) {
            const schema = await getCompiledSchema(kbId, ds.id);
            // null（未编译）不进缓存：下次点建议可重试
            if (schema !== null) schemaCacheRef.current.set(cacheKey, schema);
          }
        } catch {
          failed += 1;   // 单源失败 → 跳过该源，不拖垮整次建议
          return;
        }
        const schema = schemaCacheRef.current.get(cacheKey) ?? null;
        for (const e of schema?.entity_types ?? []) {
          candidates.push({
            dataSourceId: ds.id, sourceName: ds.name, name: String(e.name),
            ...(e.display_name ? { displayName: String(e.display_name) } : {}),
            ...(e.aliases?.length ? { aliases: e.aliases.map(String) } : {}),
          });
        }
      }));

      setSuggestPartial(failed);
      if (failed > 0 && failed === dsList.length) {
        setSuggestError(t('abstractEditor.suggestAllFailed'));
        return;
      }
      setSuggestGroups(suggestEntityLandings(entity, candidates, synonymIndex));
    } catch (e) {
      setSuggestError(e instanceof Error ? e.message : String(e));
    } finally {
      setSuggestLoading(false);
    }
  };

  const toggleWeak = (dsId: string) => setWeakExpanded((prev) => {
    const next = new Set(prev);
    if (next.has(dsId)) next.delete(dsId); else next.add(dsId);
    return next;
  });

  const confirmSuggestLandings = () => {
    const chosen = suggestGroups
      .flatMap((g) => [...g.strong, ...g.weak])
      .filter((s) => pickedSuggest.includes(suggestionKey(s)));
    if (chosen.length === 0) return;
    updateLandings([
      ...(landings ?? []),
      ...chosen.map((s) => ({
        dataSourceId: s.dataSourceId,
        entityName: s.entityName,
        included: true,
        sourceName: sources.find((d) => d.id === s.dataSourceId)?.name ?? s.dataSourceId,
        stale: false,
        staleReason: null,
      })),
    ]);
    setSuggestOpen(false);
  };

  const hitCounts = (items: EntitySuggestion[]): string => {
    const counts = new Map<HitKind, number>();
    for (const it of items) counts.set(it.hit, (counts.get(it.hit) ?? 0) + 1);
    return [...counts.entries()]
      .sort((a, b) => HIT_ORDER[a[0]] - HIT_ORDER[b[0]])
      .map(([hit, n]) => `${t(hitLabelKey(hit))} ${n}`)
      .join(' / ');
  };

  const toggleLanding = (dataSourceId: string, entityName: string, included: boolean) => {
    updateLandings((landings ?? []).map((l) =>
      l.dataSourceId === dataSourceId && l.entityName === entityName ? { ...l, included } : l,
    ));
  };

  const removeLanding = (dataSourceId: string, entityName: string) => {
    updateLandings((landings ?? []).filter(
      (l) => !(l.dataSourceId === dataSourceId && l.entityName === entityName),
    ));
  };

  const openPicker = async () => {
    setPickerOpen(true);
    await ensureSources();
  };

  const onPickSource = async (dsId: string) => {
    setPickedSource(dsId);
    setLoadingEntities(true);
    setSourceEntities([]);
    try {
      const schema = await getCompiledSchema(kbId, dsId);
      setSourceEntities((schema?.entity_types ?? []).map((e) => e.name));
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingEntities(false);
    }
  };

  const addAsNewEntity = (dsId: string, entityName: string) => {
    const name = entityName;
    // 大小写不敏感——与 `commands.ts` 的 `entity.add` 去重**同口径**。用严格 includes
    // 会漏判（源里 `user`、画布已有 `User`）：这里放行、命令层拒绝 → 点下去静默无事发生。
    if (entityNames.some((existing) => existing.toLowerCase() === name.toLowerCase())) {
      message.warning(t('abstractEditor.nameTaken', { name }));
      return;
    }
    const dsName = sources.find((s) => s.id === dsId)?.name ?? dsId;
    execute(
      {
        type: 'entity.add',
        entity: { id: `e:${name}`, name, aliases: [], attributes: [], landings: [
          { dataSourceId: dsId, entityName, included: true, sourceName: dsName, stale: false, staleReason: null },
        ] },
        position: { x: 80, y: 80 },
      },
      { kind: 'entity', id: `e:${name}` },
    );
    setPickerOpen(false);
  };

  const addLanding = (dsId: string, entityName: string) => {
    if (landings.some((l) => l.dataSourceId === dsId && l.entityName === entityName)) {
      setPickerOpen(false);
      return;
    }
    const dsName = sources.find((s) => s.id === dsId)?.name ?? dsId;
    updateLandings([
      ...(landings ?? []),
      { dataSourceId: dsId, entityName, included: true, sourceName: dsName, stale: false, staleReason: null },
    ]);
    setPickerOpen(false);
  };

  return (
    <div className="ae-landings">
      <div className="ae-landings-head">
        <span>{t('abstractEditor.landingsTitle')}</span>
        <Button size="small" type="link" icon={<BulbOutlined />} disabled={readOnly}
          onClick={() => void openSuggestLandings()}>{t('abstractEditor.suggestLandings')}</Button>
        <Button size="small" type="link" icon={<PlusOutlined />} disabled={readOnly} onClick={() => void openPicker()}>
          {t('abstractEditor.addLanding')}
        </Button>
      </div>
      {landings.length === 0 ? (
        <p className="ae-landings-empty">{t('abstractEditor.noLanding')}</p>
      ) : landings.map((l) => (
        <div key={`${l.dataSourceId}:${l.entityName}`} className={`ae-landing ${l.included ? '' : 'is-off'}`}>
          <Checkbox
            checked={l.included}
            disabled={readOnly}
            onChange={(e) => toggleLanding(l.dataSourceId, l.entityName, e.target.checked)}
          />
          <Tag className="ae-landing-src">{l.sourceName || l.dataSourceId}</Tag>
          <code>{l.entityName}</code>
          {l.stale ? (
            <Tooltip title={t(`abstractEditor.stale.${l.staleReason ?? 'entity_gone'}`)}>
              <Tag color="warning">{t('abstractEditor.staleTag')}</Tag>
            </Tooltip>
          ) : null}
          {canOpenSourceCanvas(l) ? (
            // 查看不受只读（active）影响：只闸编辑，不闸跳转（与 PropertiesPanel 的只读口径一致）。
            <Button size="small" type="link" className="ae-landing-open"
              onClick={() => navigate(sourceCanvasPath(kbId, l.dataSourceId))}>
              {t('abstractEditor.openSourceCanvas')}
            </Button>
          ) : null}
          <Button size="small" type="text" danger disabled={readOnly} onClick={() => removeLanding(l.dataSourceId, l.entityName)}>
            {t('common.delete')}
          </Button>
        </div>
      ))}

      <Modal
        open={pickerOpen}
        title={t('abstractEditor.pickerTitle')}
        footer={null}
        onCancel={() => setPickerOpen(false)}
        destroyOnHidden
      >
        <Select
          style={{ width: '100%' }}
          placeholder={t('abstractEditor.pickSource')}
          value={pickedSource}
          onChange={(v) => void onPickSource(v)}
          options={sources.map((s) => ({ value: s.id, label: s.name }))}
        />
        {pickedSource ? (
          <div className="ae-picker-list">
            {loadingEntities ? <p>{t('common.loading')}</p> : sourceEntities.map((name) => (
              <div key={name} className="ae-picker-row">
                <code>{name}</code>
                <span>
                  <Button
                    size="small"
                    onClick={() => addLanding(pickedSource, name)}
                    disabled={landings.some((l) => l.dataSourceId === pickedSource && l.entityName === name)}
                  >
                    {t('abstractEditor.addAsLanding')}
                  </Button>
                  <Button size="small" type="primary" ghost onClick={() => addAsNewEntity(pickedSource, name)}>
                    {t('abstractEditor.addAsNewEntity')}
                  </Button>
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={suggestOpen}
        title={t('abstractEditor.suggestTitle')}
        footer={null}
        onCancel={() => setSuggestOpen(false)}
        destroyOnHidden
      >
        {/* 降级提示放**弹窗顶部、三种状态之外**：同义词表挂了又恰好没有名字命中时，
            用户若只看到「没有候选」就无从知道桥根本没接上（spec §8 要求「把少了什么如实告诉用户」）。 */}
        {suggestFallback ? (
          <p className="ae-landings-empty">{t('abstractEditor.suggestFallback')}</p>
        ) : null}
        {suggestPartial > 0 ? (
          <p className="ae-landings-empty">
            {t('abstractEditor.suggestPartial', { count: suggestPartial })}
          </p>
        ) : null}
        {suggestLoading ? (
          <p className="ae-landings-empty">{t('abstractEditor.suggestLoading')}</p>
        ) : suggestError !== null ? (
          <>
            <p className="ae-landings-empty" role="alert">
              {`${t('abstractEditor.suggestError')}：${suggestError}`}
            </p>
            <Button block onClick={() => void openSuggestLandings()}>
              {t('abstractEditor.suggestRetry')}
            </Button>
          </>
        ) : suggestGroups.length === 0 ? (
          <p className="ae-landings-empty">{t('abstractEditor.suggestEmpty')}</p>
        ) : (
          <>
            {suggestGroups.map((group) => (
              <div className="ae-suggest-group" key={group.dataSourceId}>
                <div className="ae-suggest-group-head">
                  {t('abstractEditor.suggestGroupHead', {
                    source: group.sourceName || group.dataSourceId,
                    // 只有弱档候选的源：strong 为空 → hitCounts 返回空串，组头会变成「源名 · 」
                    // 尾巴挂个空。回落到 weak 计数——分组存在即至少有一档非空，所以结果必非空。
                    counts: hitCounts(group.strong) || hitCounts(group.weak),
                  })}
                </div>
                {[...group.strong, ...(weakExpanded.has(group.dataSourceId) ? group.weak : [])]
                  .map((item) => {
                    const k = suggestionKey(item);
                    return (
                      <Checkbox
                        key={k}
                        checked={pickedSuggest.includes(k)}
                        onChange={(event) => setPickedSuggest((prev) => (
                          event.target.checked ? [...prev, k] : prev.filter((x) => x !== k)))}
                      >
                        <code>{item.entityName}</code>
                        <Tag className="ae-suggest-hit">{t(hitLabelKey(item.hit))}</Tag>
                      </Checkbox>
                    );
                  })}
                {group.weak.length > 0 ? (
                  <Button type="link" size="small" onClick={() => toggleWeak(group.dataSourceId)}>
                    {weakExpanded.has(group.dataSourceId)
                      ? t('abstractEditor.suggestWeakCollapse')
                      : t('abstractEditor.suggestWeakMore', { count: group.weak.length })}
                  </Button>
                ) : null}
              </div>
            ))}
            <Button type="primary" block disabled={pickedSuggest.length === 0}
              onClick={confirmSuggestLandings}>
              {t('abstractEditor.suggestAdd', { count: pickedSuggest.length })}
            </Button>
          </>
        )}
      </Modal>
    </div>
  );
}
