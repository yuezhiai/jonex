/**
 * 指标面板（抽象编辑器专用，装在 Drawer 里）。
 *
 * 指标是跨源口径的登记：一条指标挂 N 个落点（每数据源一行），`di_metric` 走
 * DI `/v1/query`、`entity_name` 走 ② 补位。受控组件——状态住在编辑页，
 * 保存随页面「保存/转为草稿」一并提交。数据源名单拉不到不阻塞编辑
 * （下拉空、手输仍可；未知值原样保留）。
 */
import { Button, Card, Input, Select, Space, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useEffect, useState } from 'react';
import type { AbstractMetric } from '../../api/abstractSchema';
import { listDataSources } from '@/api/dataSource';   // 同目录 LandingSection.tsx:21 同款；元素形状 { id, name }
import { compiledSchemaSources } from '@/utils/dataSourceDisplay';

const ADDITIVITY_OPTIONS = [
  { value: 'additive', label: 'additive（可加）' },
  { value: 'semi_additive', label: 'semi_additive（半可加，不出合计）' },
  { value: 'non_additive', label: 'non_additive（不可加，不出合计）' },
  { value: 'unknown', label: 'unknown（未声明）' },
];

interface Props {
  kbId: string;
  value: AbstractMetric[];
  onChange: (next: AbstractMetric[]) => void;
  disabled?: boolean;
}

export default function MetricsPanel({ kbId, value, onChange, disabled }: Props) {
  const { t } = useTranslation();
  const [dsOptions, setDsOptions] = useState<{ value: string; label: string }[]>([]);

  useEffect(() => {
    let alive = true;
    void listDataSources(kbId)
      .then((list) => {
        if (!alive) return;
        // ETL 来源没有自己的编译 schema，不能当指标落点目标；其余类型原样保留。
        setDsOptions(compiledSchemaSources(list || []).map((d: any) => ({ value: d.id, label: d.name || d.id })));
      })
      .catch(() => undefined);      // ds 名单拉不到不阻塞编辑：下拉空、手输仍可（未知值原样保留）
    return () => { alive = false; };
  }, [kbId]);

  const patch = (idx: number, part: Partial<AbstractMetric>) => {
    onChange(value.map((m, i) => (i === idx ? { ...m, ...part } : m)));
  };
  const patchLanding = (mi: number, li: number, part: Partial<AbstractMetric['landings'][number]>) => {
    onChange(value.map((m, i) => i === mi
      ? { ...m, landings: m.landings.map((l, j) => (j === li ? { ...l, ...part } : l)) }
      : m));
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {value.map((metric, mi) => (
        <Card key={mi} size="small"
          title={<Input value={metric.name} disabled={disabled}
            placeholder={t('abstractEditor.metricNamePlaceholder')}
            onChange={(e) => patch(mi, { name: e.target.value })} style={{ maxWidth: 240 }} />}
          extra={<Button danger size="small" disabled={disabled} icon={<DeleteOutlined />}
            onClick={() => onChange(value.filter((_, i) => i !== mi))} />}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Input addonBefore={t('abstractEditor.metricDisplayName')} value={metric.displayName || ''}
              disabled={disabled} onChange={(e) => patch(mi, { displayName: e.target.value })} />
            <Select mode="tags" style={{ width: '100%' }} disabled={disabled}
              placeholder={t('abstractEditor.metricAliasesPlaceholder')}
              value={metric.aliases} onChange={(v: string[]) => patch(mi, { aliases: v })} />
            <Input.TextArea rows={2} value={metric.description || ''} disabled={disabled}
              placeholder={t('abstractEditor.metricDescriptionPlaceholder')}
              onChange={(e) => patch(mi, { description: e.target.value })} />
            <Select style={{ width: 320 }} disabled={disabled}
              value={metric.additivity}
              options={ADDITIVITY_OPTIONS}
              onChange={(v) => patch(mi, { additivity: v as AbstractMetric['additivity'] })} />
            <Typography.Text type="secondary">{t('abstractEditor.metricLandings')}</Typography.Text>
            {metric.landings.map((landing, li) => (
              <Space key={li} align="baseline">
                <Select showSearch style={{ width: 200 }} disabled={disabled}
                  placeholder={t('abstractEditor.metricLandingDs')}
                  value={landing.dataSourceId || undefined}
                  options={dsOptions}
                  onChange={(v) => patchLanding(mi, li, { dataSourceId: v })}
                  notFoundContent={<span>{t('abstractEditor.metricLandingDsManual')}</span>} />
                <Input placeholder="di_metric" value={landing.diMetric} disabled={disabled}
                  onChange={(e) => patchLanding(mi, li, { diMetric: e.target.value })} />
                <Input placeholder="entity_name" value={landing.entityName} disabled={disabled}
                  onChange={(e) => patchLanding(mi, li, { entityName: e.target.value })} />
                <Button danger size="small" disabled={disabled} icon={<DeleteOutlined />}
                  onClick={() => patch(mi, { landings: metric.landings.filter((_, j) => j !== li) })} />
              </Space>
            ))}
            <Button size="small" disabled={disabled} icon={<PlusOutlined />}
              onClick={() => patch(mi, { landings: [...metric.landings, { dataSourceId: '', diMetric: '', entityName: '' }] })}>
              {t('abstractEditor.metricAddLanding')}
            </Button>
          </Space>
        </Card>
      ))}
      <Button type="dashed" disabled={disabled} icon={<PlusOutlined />}
        onClick={() => onChange([...value, { name: '', aliases: [], additivity: 'unknown', landings: [] }])}>
        {t('abstractEditor.metricAdd')}
      </Button>
      <Typography.Text type="secondary">{t('abstractEditor.metricHint')}</Typography.Text>
    </Space>
  );
}
