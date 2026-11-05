/** 诊断抽屉归因段「采纳为概念别名」表单：概念下拉来自该 KB 抽象 schema（GET /concept-options），
 * 采纳走桥接 POST /dislikes/{id}/adopt-alias（subprocess 调 CLI，含备份/乐观锁/回读/反馈标记）。
 * 单步提交（无预览，镜像 MetricAdoptForm 的 spec v1 口径）。 */
import { Alert, Button, Input, Select, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { adoptConceptAlias, getConceptOptions, type ConceptOption } from '../../../api/evolution';

interface Props {
  kb: string;           // 反馈所属 KB（抽屉从 context.feedback.kb_id 传入）
  feedbackId: string;   // 采纳的幂等键 = feedback_id
}

export default function ConceptAdoptForm({ kb, feedbackId }: Props) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<ConceptOption[]>([]);
  const [concept, setConcept] = useState<string>();
  const [alias, setAlias] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!kb) { setOptions([]); return; }
    let alive = true;
    void getConceptOptions(kb)
      .then((res) => { if (alive) setOptions(res?.concepts || []); })
      .catch(() => { if (alive) setOptions([]); });
    return () => { alive = false; };
  }, [kb]);

  const submit = async () => {
    if (!concept || !alias.trim()) return;
    setBusy(true); setResult(null);
    try {
      const res = await adoptConceptAlias(feedbackId, { concept, alias: alias.trim() });
      const ok = !!res?.ok;
      const out = (res?.stdout_tail || '').trim();
      const err = (res?.stderr_tail || '').trim();
      const base = ok ? (out || t('evolutionConsole.conceptAdoptDone'))
                      : (err || out || t('evolutionConsole.conceptAdoptFailed'));
      setResult({ ok, text: ok ? `${base}\n${t('evolutionConsole.conceptAdoptVerifyHint')}` : base });
    } catch (e: any) {
      setResult({ ok: false, text: e?.message || String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!kb) return null;
  const selected = options.find((o) => o.name === concept);
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Typography.Text type="secondary">{t('evolutionConsole.conceptAdoptHint')}</Typography.Text>
      <Space>
        <Select style={{ width: 300 }} placeholder={t('evolutionConsole.conceptSelectPlaceholder')}
          value={concept} onChange={setConcept} showSearch popupMatchSelectWidth={440}
          options={options.map((o) => ({
            value: o.name,
            label: (
              <span>
                {o.name}（{o.aliases.length ? o.aliases.join('、') : '—'}）
                {o.has_landing === false && (
                  <Tag color="warning" style={{ marginLeft: 6 }}>
                    {t('evolutionConsole.conceptNoLandingTag')}
                  </Tag>
                )}
              </span>
            ),
          }))} />
        <Input style={{ width: 200 }} placeholder={t('evolutionConsole.conceptAliasPlaceholder')}
          value={alias} onChange={(e) => setAlias(e.target.value)} />
        <Button type="primary" loading={busy} disabled={!concept || !alias.trim()} onClick={submit}>
          {t('evolutionConsole.conceptAdoptSubmit')}
        </Button>
      </Space>
      {selected?.has_landing === false && (
        <Alert type="warning" showIcon
          message={t('evolutionConsole.conceptNoLandingWarning')} />
      )}
      {result && (
        <Alert type={result.ok ? 'success' : 'warning'} showIcon
          message={result.text} style={{ whiteSpace: 'pre-wrap' }} />
      )}
    </Space>
  );
}
