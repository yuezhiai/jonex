/** 失败行展开区「采纳为指标别名」表单：指标下拉来自该 KB 的抽象 schema（GET /metric-options），
 * 采纳走桥接 POST /failures/adopt-metric（subprocess 调 CLI，含登录+回读）。单步提交（无预览，spec v1）。
 * kb 取不到（空串）时整体不渲染——由父组件（FailuresTab）从失败行解析后传入。 */
import { Alert, Button, Input, Select, Space, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { adoptMetricAlias, getMetricOptions, type MetricOption } from '../../../api/evolution';

interface Props {
  kb: string;               // 失败行所属 KB（由父组件从 row 解析后传入）
}

export default function MetricAdoptForm({ kb }: Props) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<MetricOption[]>([]);
  const [metric, setMetric] = useState<string>();
  const [alias, setAlias] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!kb) { setOptions([]); return; }
    let alive = true;
    void getMetricOptions(kb)
      .then((res) => { if (alive) setOptions(res?.metrics || []); })
      .catch(() => { if (alive) setOptions([]); });
    return () => { alive = false; };
  }, [kb]);

  const adoptId = useMemo(
    () => (metric && alias.trim() ? `metric-${kb}-${metric}-${alias.trim()}` : ''),
    [kb, metric, alias],
  );

  const submit = async () => {
    if (!metric || !alias.trim()) return;
    setBusy(true); setResult(null);
    try {
      const res = await adoptMetricAlias({ id: adoptId, metric, alias: alias.trim(), kb });
      const ok = !!res?.ok;
      const out = (res?.stdout_tail || '').trim();
      const err = (res?.stderr_tail || '').trim();
      setResult({
        ok,
        text: ok ? (out || t('evolutionConsole.metricAdoptDone'))
                 : (err || out || t('evolutionConsole.metricAdoptFailed')),
      });
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!kb) return null;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Typography.Text type="secondary">{t('evolutionConsole.metricAdoptHint')}</Typography.Text>
      <Space>
        <Select style={{ width: 220 }} placeholder={t('evolutionConsole.metricSelectPlaceholder')}
          value={metric} onChange={setMetric} showSearch
          options={options.map((o) => ({ value: o.name, label: `${o.name}（${o.additivity}）` }))} />
        <Input style={{ width: 220 }} placeholder={t('evolutionConsole.metricAliasPlaceholder')}
          value={alias} onChange={(e) => setAlias(e.target.value)} />
        <Button type="primary" loading={busy} disabled={!metric || !alias.trim()} onClick={submit}>
          {t('evolutionConsole.metricAdoptSubmit')}
        </Button>
      </Space>
      {result && (
        <Alert type={result.ok ? 'success' : 'warning'} showIcon
          message={result.text} style={{ whiteSpace: 'pre-wrap' }} />
      )}
    </Space>
  );
}
