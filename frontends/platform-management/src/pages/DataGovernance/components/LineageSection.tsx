import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Select, Skeleton, Space } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getAssetLineage } from '@/api/dataGovernance';
import type { DgDirection, DgLineage } from '../types';
import { errorMeta } from '../utils';
import LineageGraph from './LineageGraph';

interface Props {
  assetId: string;
}

/**
 * 资产血缘区（只处理文档，父组件已在 document 分支渲染）。
 *
 * 独立持有 direction/depth/graph 状态：抽屉按资产 key 重挂本组件，
 * 切换资产时状态自然归零，不会再出现「旧图残留」或同一资产重复请求。
 */
export default function LineageSection({ assetId }: Props) {
  const { t } = useTranslation();
  // 异步回调里读 tRef：切换语言不应触发重新取数。
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const [direction, setDirection] = useState<DgDirection>('both');
  const [depth, setDepth] = useState<1 | 2>(1);
  const [graph, setGraph] = useState<DgLineage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 请求序号：只有最后一次发起的请求允许回写状态。
  const graphSeq = useRef(0);

  const loadGraph = useCallback(async () => {
    if (!assetId) return;
    const my = ++graphSeq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await getAssetLineage('document', assetId, direction, depth);
      if (my !== graphSeq.current) return;
      setGraph(next);
    } catch (err: unknown) {
      if (my !== graphSeq.current) return;
      const meta = errorMeta(err);
      setGraph(null);
      setError(
        meta.status !== undefined && meta.message
          ? meta.message
          : tRef.current('dataGovernance.lineage.loadFailed'),
      );
    } finally {
      if (my === graphSeq.current) setLoading(false);
    }
  }, [assetId, direction, depth]);

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  // 卸载后让在途请求失效，避免对已卸载组件 setState。
  useEffect(
    () => () => {
      graphSeq.current += 1;
    },
    [],
  );

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          flexWrap: 'wrap',
        }}
      >
        <div style={{ fontWeight: 600 }}>{t('dataGovernance.lineage.title')}</div>
        <Space wrap>
          <Select
            aria-label={t('dataGovernance.lineage.direction')}
            value={direction}
            style={{ width: 120 }}
            options={[
              { value: 'both', label: t('dataGovernance.lineage.both') },
              { value: 'up', label: t('dataGovernance.lineage.up') },
              { value: 'down', label: t('dataGovernance.lineage.down') },
            ]}
            onChange={(value) => setDirection(value as DgDirection)}
          />
          <Select
            aria-label={t('dataGovernance.lineage.depth')}
            value={depth}
            style={{ width: 100 }}
            options={[
              { value: 1, label: t('dataGovernance.lineage.depth1') },
              { value: 2, label: t('dataGovernance.lineage.depth2') },
            ]}
            onChange={(value) => setDepth(value as 1 | 2)}
          />
          <Button
            size="small"
            icon={<ReloadOutlined />}
            aria-label={t('common.refresh')}
            loading={loading}
            disabled={loading}
            onClick={() => void loadGraph()}
          />
        </Space>
      </div>

      {error ? (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => void loadGraph()}
            >
              {t('common.retry')}
            </Button>
          }
        />
      ) : loading || !graph ? (
        // 首次打开时 graph 尚未取回也走 Skeleton，与详情区同口径
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : (
        <LineageGraph lineage={graph} />
      )}
    </Space>
  );
}
