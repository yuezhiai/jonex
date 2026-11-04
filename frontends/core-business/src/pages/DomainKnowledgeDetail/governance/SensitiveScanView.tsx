/** 治理 → 敏感数据分布扫描（W2.7a）：只读建议 + 人工采纳成标签。
 *
 * 口径：`docs/superpowers/specs/2026-11-01-w2.7a-sensitive-scan-design.md`。
 * 扫描只用编译 schema 已存证据；返回样例一律后端掩码；采纳写 origin=manual 的标签。
 */
import { useState } from 'react';
import {
  Alert, Button, Empty, Segmented, Space, Spin, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import {
  adoptSensitiveScan,
  scanSensitiveData,
  type ScanConfidence,
  type SensitiveAdoptItem,
  type SensitiveScanItem,
  type SensitiveScanResult,
} from '../../../api/governance';

const CONF_COLOR: Record<string, string> = {
  high: 'red', medium: 'orange', low: 'default',
};
const LEVEL_COLOR: Record<string, string> = {
  secret: 'red', confidential: 'orange', internal: 'blue', public: 'default',
};

const keyOf = (row: SensitiveScanItem) =>
  `${row.data_source_id}|${row.table}|${row.column}`;

interface Props {
  kbId: string;
  canWrite?: boolean;
}

export default function SensitiveScanView({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [minConfidence, setMinConfidence] = useState<ScanConfidence>('medium');
  const [result, setResult] = useState<SensitiveScanResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [adopting, setAdopting] = useState(false);

  const scan = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await scanSensitiveData(kbId, { min_confidence: minConfidence });
      setResult(res);
      setSelected([]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  const adopt = async () => {
    const rows = (result?.items || []).filter(
      (row) => selected.includes(keyOf(row)) && row.status !== 'labeled');
    if (!rows.length) {
      message.warning(t('domainKnowledge.governance.scanNothingSelected'));
      return;
    }
    const items: SensitiveAdoptItem[] = rows.map((row) => ({
      data_source_id: row.data_source_id,
      table: row.table,
      column: row.column,
      level: row.suggested_level,
      tags: row.suggested_tags,
    }));
    setAdopting(true);
    try {
      const res = await adoptSensitiveScan(kbId, items);
      message.success(t('domainKnowledge.governance.scanAdoptDone', {
        created: res.created, skipped: res.skipped,
      }));
      await scan();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setAdopting(false);
    }
  };

  const columns: ColumnsType<SensitiveScanItem> = [
    {
      title: t('domainKnowledge.governance.scanColTable'),
      key: 'table',
      render: (_, row) => (
        <span>
          <code>{row.table}</code>
          <span style={{ color: '#8c8c8c', marginLeft: 6, fontSize: 12 }}>
            {row.data_source_id.slice(0, 8)}
          </span>
        </span>
      ),
      ellipsis: true,
    },
    {
      title: t('domainKnowledge.governance.scanColColumn'),
      dataIndex: 'column',
      render: (v: string) => <code>{v}</code>,
      ellipsis: true,
    },
    {
      title: t('domainKnowledge.governance.scanColMatched'),
      dataIndex: 'matched',
      render: (v: string[]) => (
        <Space size={4} wrap>{(v || []).map((x) => <Tag key={x}>{x}</Tag>)}</Space>
      ),
    },
    {
      title: t('domainKnowledge.governance.scanColLevel'),
      dataIndex: 'suggested_level',
      width: 110,
      render: (v: string) => (
        <Tag color={LEVEL_COLOR[v] ?? 'default'}>
          {t(`domainKnowledge.governance.level${v.charAt(0).toUpperCase()}${v.slice(1)}`, { defaultValue: v })}
        </Tag>
      ),
    },
    {
      title: t('domainKnowledge.governance.scanColTags'),
      dataIndex: 'suggested_tags',
      render: (v: string[]) => (v || []).join(', '),
    },
    {
      title: t('domainKnowledge.governance.scanColConfidence'),
      dataIndex: 'confidence',
      width: 100,
      render: (v: string) => (
        <Tag color={CONF_COLOR[v] ?? 'default'}>
          {t(`domainKnowledge.governance.scanConfidence.${v}`, { defaultValue: v })}
        </Tag>
      ),
    },
    {
      title: t('domainKnowledge.governance.scanColSample'),
      dataIndex: 'sample_masked',
      width: 160,
      render: (v?: string) => (v ? <code>{v}</code> : '-'),
    },
    {
      title: t('domainKnowledge.governance.scanColStatus'),
      dataIndex: 'status',
      width: 130,
      render: (v: string) => (
        <Tag color={v === 'labeled' ? 'green' : v === 'derived_present' ? 'blue' : 'default'}>
          {t(`domainKnowledge.governance.scanStatus.${v}`, { defaultValue: v })}
        </Tag>
      ),
    },
  ];

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
             message={t('domainKnowledge.governance.scanHint')} />
      <Space style={{ marginBottom: 12 }} wrap>
        <Segmented
          value={minConfidence}
          onChange={(v) => setMinConfidence(v as ScanConfidence)}
          options={[
            { value: 'medium', label: t('domainKnowledge.governance.scanConfidence.medium') },
            { value: 'low', label: t('domainKnowledge.governance.scanConfidence.low') },
          ]}
        />
        <Button type="primary" loading={loading} onClick={scan}>
          {t('domainKnowledge.governance.scanButton')}
        </Button>
        <Button
          disabled={!canWrite || !selected.length}
          loading={adopting}
          onClick={adopt}
        >
          {t('domainKnowledge.governance.scanAdopt', { count: selected.length })}
        </Button>
        {result && (
          <span style={{ color: '#8c8c8c' }}>
            {t('domainKnowledge.governance.scanStats', {
              sources: result.stats.sources,
              candidates: result.stats.candidates,
            })}
          </span>
        )}
      </Space>
      {!canWrite && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
               message={t('domainKnowledge.governance.scanReadOnly')} />
      )}
      {error && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={scan}>{t('common.retry')}</Button>} />
      )}
      {loading && !result ? (
        <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>
      ) : !result ? (
        <Empty description={t('domainKnowledge.governance.scanEmpty')} />
      ) : result.items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.scanNoCandidate')} />
      ) : (
        <Table<SensitiveScanItem>
          rowKey={keyOf}
          size="small"
          dataSource={result.items}
          columns={columns}
          scroll={{ x: 1100 }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys as string[]),
            getCheckboxProps: (row) => ({ disabled: row.status === 'labeled' }),
          }}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
        />
      )}
    </div>
  );
}
