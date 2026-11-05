/**
 * 血缘覆盖率条（W2.4a，只读）。切源/边写成功后由父级 revision 触发重查。
 *
 * - `ratio=null` ⇒ 显示「—」（分母为 0 或读边失败），不是 0%；
 * - `unknown=true` ⇒ 显式告警，不展示成 0%；
 * - 无 active 编译 schema（后端 1001）⇒ 错误态 + 原样 message。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Spin, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { getLineageCoverage, type LineageCoverage } from '@/api/lineageImpact';
import { originLabel } from './lineageOriginLabels';

const { Text } = Typography;

interface Props {
  kbId: string;
  dsId: string;
  /** 边内容修订号：变化即重查 */
  revision: string;
}

function ratioText(value: number | null): string {
  return value == null ? '—' : `${Math.round(value * 100)}%`;
}

export default function LineageCoverageBar({ kbId, dsId, revision }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<LineageCoverage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const req = ++reqRef.current;
    setLoading(true);
    setError('');
    try {
      const result = await getLineageCoverage(kbId, dsId);
      if (reqRef.current === req) setData(result);
    } catch (e: any) {
      if (reqRef.current === req) {
        setData(null);
        setError(e?.message || t('domainKnowledge.lineage.coverage.loadFailed'));
      }
    } finally {
      if (reqRef.current === req) setLoading(false);
    }
  }, [kbId, dsId, t]);

  useEffect(() => {
    void load();
  }, [load, revision]);

  if (loading) {
    return (
      <div style={{ padding: 8 }}>
        <Spin size="small" /> <Text type="secondary">{t('domainKnowledge.lineage.coverage.title')}</Text>
      </div>
    );
  }

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 12 }}
        message={t('domainKnowledge.lineage.coverage.loadFailed')}
        description={error}
        action={
          <Button size="small" onClick={() => void load()}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  if (!data) return null;

  if (data.unknown) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message={t('domainKnowledge.lineage.coverage.unknown')}
      />
    );
  }

  const originEntries = Object.entries(data.by_origin);

  return (
    <div
      style={{
        marginBottom: 12,
        padding: '8px 12px',
        border: '1px solid #e2e8f0',
        borderRadius: 8,
        background: '#f8fafc',
      }}
    >
      <Space wrap size={[16, 4]}>
        <Text strong>{t('domainKnowledge.lineage.coverage.title')}</Text>
        <Text>
          {t('domainKnowledge.lineage.coverage.tables')}：
          {data.tables.covered}/{data.tables.total}（{ratioText(data.tables.ratio)}）
        </Text>
        <Text>
          {t('domainKnowledge.lineage.coverage.columns')}：
          {data.columns.covered}/{data.columns.total}（{ratioText(data.columns.ratio)}）
        </Text>
        <Space size={[4, 4]} wrap>
          <Text type="secondary">{t('domainKnowledge.lineage.coverage.origin')}：</Text>
          {originEntries.length === 0 ? (
            <Text type="secondary">—</Text>
          ) : (
            originEntries.map(([origin, count]) => (
              <Tag key={origin}>{originLabel(t, origin)}: {count}</Tag>
            ))
          )}
        </Space>
        {data.orphan_edges > 0 && (
          <Tag color="orange">
            {t('domainKnowledge.lineage.coverage.orphan', { count: data.orphan_edges })}
          </Tag>
        )}
        {data.truncated && (
          <Tag color="gold">{t('domainKnowledge.lineage.coverage.truncated')}</Tag>
        )}
      </Space>
    </div>
  );
}
