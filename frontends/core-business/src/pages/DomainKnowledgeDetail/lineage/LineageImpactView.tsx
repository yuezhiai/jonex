/**
 * 血缘影响分析视图（W2.4a，只读）。
 *
 * 只使用**血缘边**（不含 join 边）；方向/跳数/来源档语义与画布一致。
 * `unknown` 是「读边失败」，不是「没有边」——必须分开提示。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Empty,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  getLineageImpact,
  type LineageDirection,
  type LineageImpactLevel,
  type LineageImpactResult,
} from '@/api/lineageImpact';
import { originLabel } from './lineageOriginLabels';

const { Text } = Typography;

const ORIGINS = ['declared', 'manual', 'comment', 'platform', 'value', 'derived'];

interface Props {
  kbId: string;
  dsId: string;
  /** 当前源血缘边出现过的表名（父级 anchorTables） */
  tables: string[];
  /** 表 → 该表在边集里出现过的列，用于列下拉建议 */
  columnsByTable: Record<string, string[]>;
}

export default function LineageImpactView({ kbId, dsId, tables, columnsByTable }: Props) {
  const { t } = useTranslation();
  const [table, setTable] = useState<string>();
  const [column, setColumn] = useState('');
  const [direction, setDirection] = useState<LineageDirection>('downstream');
  const [hops, setHops] = useState(3);
  const [minOrigin, setMinOrigin] = useState<string>();
  const [result, setResult] = useState<LineageImpactResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const reqRef = useRef(0);

  /**
   * 作废在飞请求并回到 idle：切源、切表、以及**任一查询参数**（方向/跳数/来源档）
   * 变化都必须走这里，否则旧参数的结果会挂在新参数下面。
   */
  const resetResult = useCallback(() => {
    reqRef.current += 1;
    setResult(null);
    setError('');
    setLoading(false);
  }, []);

  useEffect(() => {
    // 切源：作废在飞请求（连同其 loading / 写回），清空锚点与结果；
    // `table` 清空后由下面的 tables effect 等**新** tables 到达再选。
    resetResult();
    setTable(undefined);
    setColumn('');
  }, [dsId, resetResult]);

  useEffect(() => {
    // 统一处理 tables 变化：不得沿用上一个源的旧表。
    // 自动切表（当前 table 已不在 tables 中，或 tables 清空）同样作废在飞请求，
    // 并清掉旧锚点的结果/错误/列/loading —— 否则旧结果会挂在新锚点下面。
    if (tables.length === 0) {
      if (table !== undefined) {
        resetResult();
        setTable(undefined);
        setColumn('');
      }
    } else if (!table || !tables.includes(table)) {
      resetResult();
      setTable(tables[0]);
      setColumn('');
    }
  }, [tables, table, resetResult]);

  const columnOptions = useMemo(
    () => (table ? (columnsByTable[table] ?? []).map((c) => ({ value: c })) : []),
    [columnsByTable, table],
  );

  const run = async () => {
    if (!table) return;
    const req = ++reqRef.current;
    setLoading(true);
    setError('');
    try {
      const data = await getLineageImpact(kbId, dsId, {
        table,
        column: column.trim() || undefined,
        direction,
        hops,
        min_origin: minOrigin || undefined,
      });
      if (reqRef.current === req) setResult(data);
    } catch (e: any) {
      if (reqRef.current === req) {
        setResult(null);
        setError(e?.message || t('domainKnowledge.lineage.impact.error'));
      }
    } finally {
      if (reqRef.current === req) setLoading(false);
    }
  };

  if (tables.length === 0) {
    return <Empty description={t('domainKnowledge.lineage.impact.noTables')} />;
  }

  const renderLevel = (level: LineageImpactLevel) => (
    <Card
      key={level.hop}
      size="small"
      title={t('domainKnowledge.lineage.impact.hop', { hop: level.hop })}
      style={{ marginBottom: 12 }}
    >
      <Space wrap size={[4, 4]}>
        {level.nodes.map((node) => (
          <Tag key={`${node.table}.${node.column ?? ''}`} color="geekblue"
               style={{ fontFamily: 'Consolas, monospace' }}>
            {node.column ? `${node.table}.${node.column}` : node.table}
          </Tag>
        ))}
      </Space>
      <div style={{ marginTop: 8 }}>
        {level.edges.map((edge) => (
          <div key={`${edge.from_table}.${edge.from_column}->${edge.to_table}.${edge.to_column}`}
               style={{ fontFamily: 'Consolas, monospace', fontSize: 12 }}>
            {edge.from_table}.{edge.from_column} {edge.reversed ? '←' : '→'}{' '}
            {edge.to_table}.{edge.to_column}
            <Tag style={{ marginInlineStart: 8 }} color="default">{originLabel(t, edge.origin)}</Tag>
          </div>
        ))}
      </div>
    </Card>
  );

  return (
    <div>
      <p className="yx-kb-graph-desc">{t('domainKnowledge.lineage.impact.hint')}</p>
      <Space wrap style={{ marginBottom: 12 }}>
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.impact.anchor')}</span>
        <Select
          value={table}
          onChange={(value) => {
            // 切锚点表同样作废在飞请求，旧表的结果不得写回新表。
            resetResult();
            setTable(value);
            setColumn('');
          }}
          options={tables.map((item) => ({ value: item, label: item }))}
          style={{ minWidth: 220 }}
          showSearch
          optionFilterProp="label"
        />
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.impact.column')}</span>
        <AutoComplete
          value={column}
          onChange={(value) => {
            // 锚点列也是查询参数：变化即回到 idle，旧列的结果不得留在新列下面。
            setColumn(value);
            resetResult();
          }}
          options={columnOptions}
          placeholder={t('domainKnowledge.lineage.impact.columnPlaceholder')}
          style={{ width: 200 }}
          allowClear
        />
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.impact.direction')}</span>
        <Select
          value={direction}
          onChange={(value) => {
            // 参数变化即回到 idle：旧方向的结果不得留在新方向下面。
            resetResult();
            setDirection(value);
          }}
          options={[
            { value: 'downstream', label: t('domainKnowledge.lineage.impact.downstream') },
            { value: 'upstream', label: t('domainKnowledge.lineage.impact.upstream') },
            { value: 'both', label: t('domainKnowledge.lineage.impact.both') },
          ]}
          style={{ width: 100 }}
        />
        <span className="yx-kb-meta-label">{t('domainKnowledge.lineage.graph.hops')}</span>
        <Select
          value={hops}
          onChange={(value) => {
            resetResult();
            setHops(value);
          }}
          options={[1, 2, 3].map((value) => ({
            value, label: t('domainKnowledge.lineage.impact.hopValue', { count: value }),
          }))}
          style={{ width: 90 }}
        />
        <Select
          value={minOrigin}
          onChange={(value) => {
            resetResult();
            setMinOrigin(value);
          }}
          options={ORIGINS.map((origin) => ({ value: origin, label: originLabel(t, origin) }))}
          placeholder={t('domainKnowledge.lineage.impact.minOriginAny')}
          style={{ width: 120 }}
          allowClear
        />
        <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={() => void run()}>
          {t('domainKnowledge.lineage.impact.analyze')}
        </Button>
      </Space>

      {loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={t('domainKnowledge.lineage.impact.error')}
          description={error}
          action={<Button size="small" onClick={() => void run()}>{t('common.retry')}</Button>}
        />
      ) : !result ? (
        <Empty description={t('domainKnowledge.lineage.impact.idle')} />
      ) : result.unknown ? (
        <Alert type="warning" showIcon message={t('domainKnowledge.lineage.impact.unknown')} />
      ) : result.levels.length === 0 ? (
        <Empty description={t('domainKnowledge.lineage.impact.empty')} />
      ) : (
        <div>
          <Space style={{ marginBottom: 12 }} wrap>
            <Text strong>
              {t('domainKnowledge.lineage.impact.affected', {
                tables: result.affected.tables.length,
                columns: result.affected.columns.length,
              })}
            </Text>
            <Tag>{t('domainKnowledge.lineage.impact.edgeCount', { count: result.total_edges })}</Tag>
          </Space>
          {result.truncated && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={t('domainKnowledge.lineage.impact.truncated')}
            />
          )}
          {result.levels.map(renderLevel)}
        </div>
      )}
    </div>
  );
}
