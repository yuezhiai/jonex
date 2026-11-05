/**
 * 「概念对齐」页签内的「数据资产对齐」区（P1.5b）：文档侧实体类型 ↔ 数据源实体。
 *
 * 后端只把同名/别名命中的候选落成 `proposed` —— **人审 `active` 之后才投影
 * `MAPS_TO`**，所以这里的写操作都是审批语义，**只受写权限**约束
 * （`readOnly = !canWrite`，由父组件传入）—— 抽象 schema 是否为 active 与审批无关。
 *
 * 「生成建议」是唯一例外：后端 `_load_concepts` 只从 **active** 抽象 schema 取概念，
 * schema 未确认时生成必然 0 候选。因此它另受 `suggestDisabled` 门控（无写权限，
 * 或 schema 不是 active），并把原因**常显**在卡片里（不是只藏在 tooltip 里）——
 * 无 schema 分支同样渲染本区，用户必须看得到「先确认抽象 schema」。
 *
 * 写操作按行记 in-flight（`Set<string>`）：同一行双击仍被 `busy` 挡住，但点 A 再点 B
 * 不会把 A 的忙碌态提前解除（A 的 PATCH 还在路上）。
 *
 * 四态齐：loading / empty（显式「还没有建议」）/ error（后端 message 原文）/
 * success。生成建议后额外显示一行 `created/existing/unresolved` ——
 * `unresolved > 0` 表示有文档实体类型一个可解析候选都没有，不能当成「没有」。
 *
 * 本区刻意保持紧凑，不改动下方概念卡片的结构与行为。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Empty, Select, Space, Spin, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import {
  listAlignments,
  setAlignmentStatus,
  suggestAlignments,
  type AlignmentListResult,
  type AlignmentStatus,
  type OntologyDataAlignment,
} from '@/api/ontologyAlignment';
import {
  ALIGNMENT_STATUS_ORDER,
  groupByStatus,
  suggestionSummary,
  type SuggestionSummary,
} from './dataAlignmentModel';

const { Text } = Typography;

type FilterValue = 'all' | AlignmentStatus;

interface Props {
  kbId: string;
  /** 写权限门控：**只由 `!canWrite` 决定**，控制启用/拒绝/改回候选。 */
  readOnly: boolean;
  /**
   * 「生成建议」的独立门控：无写权限，**或**抽象 schema 尚未确认（非 `active`）。
   * 后端只从 active 概念生成候选，未确认时生成必然 0 条，不该让用户白点。
   */
  suggestDisabled: boolean;
}

/** 状态 → antd Tag 颜色；未知状态用 default（不猜语义）。 */
function statusColor(status: string): string {
  if (status === 'active') return 'green';
  if (status === 'rejected') return 'red';
  if (status === 'proposed') return 'orange';
  return 'default';
}

export default function DataAlignmentSection({ kbId, readOnly, suggestDisabled }: Props) {
  const { t } = useTranslation();

  const [items, setItems] = useState<OntologyDataAlignment[]>([]);
  /** KB 级 schema 新鲜度（读时判定）：`missing` / `outdated` 在数据态显式提示。 */
  const [freshness, setFreshness] = useState<AlignmentListResult['freshness']>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [suggesting, setSuggesting] = useState(false);
  // 每行各自记 in-flight：点 A 再点 B 不会把 A 提前解除忙碌（A 的 PATCH 还在路上）；
  // 同一行连续点击仍由 `busy` 挡住。
  const [updatingIds, setUpdatingIds] = useState<Set<string>>(() => new Set());
  /** data_source_id → 展示名。拉取失败时保留空表，行上回退为短 id（title 仍是全 id）。 */
  const [sourceNames, setSourceNames] = useState<Record<string, string>>({});
  const [summary, setSummary] = useState<SuggestionSummary | null>(null);
  const [filter, setFilter] = useState<FilterValue>('all');

  // 数据源名字：与 LineageTab 同一来源（`getDomainKnowledgeDataSources`）。这是纯展示
  // 增强 —— 失败就静默回退到 id，不能让整个对齐区变成 error 态。
  useEffect(() => {
    let cancelled = false;
    getDomainKnowledgeDataSources(kbId, t)
      .then((list) => {
        if (cancelled) return;
        const map: Record<string, string> = {};
        for (const ds of list) map[ds.id] = ds.name;
        setSourceNames(map);
      })
      .catch(() => {
        /* 回退到短 id（见 sourceLabel） */
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, t]);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      // `?? []`：接口契约回 `{items, freshness}`，但 mock / 老后端缺字段时不能让 `items.length` 炸整页
      const { items: rows, freshness: nextFreshness } = await listAlignments(kbId);
      setItems(rows ?? []);
      setFreshness(nextFreshness);
    } catch (e: any) {
      // 后端 message 原文优先（「KB 不存在」这类原因本地文案说不出来）
      setError(e?.message || t('domainKnowledge.dataAlignment.loadError'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 生成建议。建议已写库成功，**后面的重拉失败不能反过来报「生成失败」**
   * （与 `AbstractSchemaTab.doGenerate` 同一个坑）：`load()` 自己吞异常、
   * 只落 error 态，摘要照常显示。
   */
  const doSuggest = async () => {
    setSuggesting(true);
    try {
      const result = await suggestAlignments(kbId);
      setSummary(suggestionSummary(result));
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.dataAlignment.suggestFailed'));
    } finally {
      setSuggesting(false);
    }
  };

  const changeStatus = async (row: OntologyDataAlignment, status: AlignmentStatus) => {
    if (readOnly) return;
    setUpdatingIds((prev) => new Set(prev).add(row.id));
    try {
      const updated = await setAlignmentStatus(kbId, row.id, status);
      // 用回包整行替换；回包缺字段时保留原值（后端 PATCH 回整行，这里只是防御）
      setItems((prev) =>
        prev.map((x) => (x.id === row.id ? { ...x, ...updated, id: x.id } : x)),
      );
      message.success(t('domainKnowledge.dataAlignment.updated'));
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.dataAlignment.updateFailed'));
    } finally {
      // 只摘掉这一行：别的行可能仍在 PATCH 中
      setUpdatingIds((prev) => {
        const next = new Set(prev);
        next.delete(row.id);
        return next;
      });
    }
  };

  /**
   * 数据源展示名：有名字用名字，没有则缩短 id（全 id 始终挂在 `title` 上）。
   * 裸 32 位 UUID 在行里既占宽又读不出信息；缩到 8 位至少能区分不同源。
   */
  const sourceLabel = (id: string): string => {
    const name = sourceNames[id];
    if (name) return name;
    return id.length > 8 ? `${id.slice(0, 8)}…` : id;
  };

  const statusLabel = (status: string) => {
    if (status === 'proposed') return t('domainKnowledge.dataAlignment.statusProposed');
    if (status === 'active') return t('domainKnowledge.dataAlignment.statusActive');
    if (status === 'rejected') return t('domainKnowledge.dataAlignment.statusRejected');
    // 未知状态原样显示：宁可露出一串不认识的英文，也不把它伪装成三态之一
    return status;
  };

  // 过滤在客户端做（一次拉全量）：分组头要带各状态计数，按状态重拉会让计数失真
  const groups = useMemo(
    () => groupByStatus(filter === 'all' ? items : items.filter((x) => x.status === filter)),
    [items, filter],
  );

  const filterOptions = useMemo(
    () => [
      { value: 'all' as FilterValue, label: t('domainKnowledge.dataAlignment.filterAll') },
      ...ALIGNMENT_STATUS_ORDER.map((s) => ({ value: s as FilterValue, label: statusLabel(s) })),
    ],
    // statusLabel 只是 t 的包装；t 变化时这里也应重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t],
  );

  const rowActions = (row: OntologyDataAlignment) => {
    const busy = updatingIds.has(row.id);
    const disabled = readOnly || busy;
    return (
      <Space size={0} style={{ marginLeft: 'auto' }}>
        {row.status !== 'active' && (
          <Button
            type="link"
            size="small"
            disabled={disabled}
            onClick={() => void changeStatus(row, 'active')}
          >
            {t('domainKnowledge.dataAlignment.activate')}
          </Button>
        )}
        {row.status !== 'rejected' && (
          <Button
            type="link"
            size="small"
            danger
            disabled={disabled}
            onClick={() => void changeStatus(row, 'rejected')}
          >
            {t('domainKnowledge.dataAlignment.reject')}
          </Button>
        )}
        {row.status !== 'proposed' && (
          <Button
            type="link"
            size="small"
            disabled={disabled}
            onClick={() => void changeStatus(row, 'proposed')}
          >
            {t('domainKnowledge.dataAlignment.revert')}
          </Button>
        )}
      </Space>
    );
  };

  /**
   * 新鲜度提示。只在**数据态**（empty / success）渲染：loading 中不显示上一次的旧提示，
   * error 时错误优先（读失败的新鲜度没有意义）。由 `body()` 统一插入。
   */
  const freshnessHint =
    freshness === 'outdated' ? (
      <Alert
        type="warning"
        showIcon
        message={t('domainKnowledge.dataAlignment.freshnessOutdated')}
      />
    ) : freshness === 'missing' ? (
      <Alert
        type="info"
        showIcon
        message={t('domainKnowledge.dataAlignment.freshnessMissing')}
      />
    ) : null;

  const body = () => {
    if (loading) {
      return <div style={{ padding: 8, textAlign: 'center' }}><Spin size="small" /></div>;
    }
    if (error) {
      return (
        <Alert
          type="error"
          showIcon
          message={t('domainKnowledge.dataAlignment.loadError')}
          description={error}
        />
      );
    }
    if (items.length === 0) {
      return (
        <>
          {freshnessHint}
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t('domainKnowledge.dataAlignment.empty')}
          />
        </>
      );
    }
    return (
      <>
        {freshnessHint}
        <Space wrap size={8} style={{ marginBottom: 4 }}>
          <Text type="secondary">{t('domainKnowledge.dataAlignment.filterStatus')}</Text>
          <Select<FilterValue>
            size="small"
            value={filter}
            options={filterOptions}
            style={{ width: 128 }}
            onChange={setFilter}
          />
        </Space>
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {groups.map((group) => (
            <div key={group.status} style={{ width: '100%' }}>
              <Space size={6} style={{ marginBottom: 2 }}>
                <Tag color={statusColor(group.status)}>{statusLabel(group.status)}</Tag>
                <Text type="secondary">{group.items.length}</Text>
              </Space>
              {group.items.map((row) => (
                <div
                  key={row.id}
                  style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    alignItems: 'center',
                    gap: 8,
                    padding: '4px 0',
                    borderTop: '1px solid #f0f0f0',
                  }}
                >
                  <Text strong>{row.doc_type}</Text>
                  {/* 三态：只有 `stale === true` 才提示；`false` / `null`（无法判定）不显示 */}
                  {row.stale === true ? (
                    <Tag color="warning">
                      {t('domainKnowledge.dataAlignment.staleDocType')}
                    </Tag>
                  ) : null}
                  <Text type="secondary">↔</Text>
                  {/* 展示数据源名（拉不到时缩 id），完整 id 始终在 title 上可查 */}
                  <Tag title={row.data_source_id}>{sourceLabel(row.data_source_id)}</Tag>
                  <Text code>{row.entity_name}</Text>
                  <Tag color={statusColor(row.status)}>{statusLabel(row.status)}</Tag>
                  {rowActions(row)}
                </div>
              ))}
            </div>
          ))}
        </Space>
      </>
    );
  };

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <Text strong>{t('domainKnowledge.dataAlignment.title')}</Text>
          {items.length > 0 && (
            <Text type="secondary">
              {t('domainKnowledge.dataAlignment.count', { count: items.length })}
            </Text>
          )}
        </Space>
      }
      extra={
        <Space>
          <Button
            size="small"
            type="primary"
            loading={suggesting}
            disabled={suggestDisabled}
            title={
              suggestDisabled && !readOnly
                ? t('domainKnowledge.dataAlignment.suggestNeedsActive')
                : undefined
            }
            onClick={() => void doSuggest()}
          >
            {t('domainKnowledge.dataAlignment.suggest')}
          </Button>
          <Button size="small" onClick={() => void load()}>
            {t('domainKnowledge.dataAlignment.refresh')}
          </Button>
        </Space>
      }
      style={{ width: '100%' }}
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Text type="secondary">{t('domainKnowledge.dataAlignment.hint')}</Text>
        {summary && (
          <Text type="secondary">
            {t('domainKnowledge.dataAlignment.suggestResult', {
              created: summary.created,
              existing: summary.existing,
              unresolved: summary.unresolved,
            })}
          </Text>
        )}
        {readOnly && (
          <Text type="secondary">{t('domainKnowledge.dataAlignment.readOnlyHint')}</Text>
        )}
        {/* 常显（不是只藏在 tooltip）：无 schema 分支没有别的提示位，用户必须
            当场知道「生成建议」为何不可点 —— 要先确认抽象 schema。 */}
        {!readOnly && suggestDisabled && (
          <Text type="secondary">{t('domainKnowledge.dataAlignment.suggestNeedsActive')}</Text>
        )}
        {body()}
      </Space>
    </Card>
  );
}
