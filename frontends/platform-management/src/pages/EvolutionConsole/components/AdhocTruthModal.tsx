import React, { useEffect, useState } from 'react';
import { Alert, Input, Modal, Select, Space, Tag, Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import type { AdhocCase, AdhocTruth } from '../../../api/evolution';

interface DraftRow {
  control: string;
  expect: 'answer' | 'refuse';
}

interface AdhocTruthModalProps {
  open: boolean;
  /** 预览返回的待补 truth case（`AdoptPreview.adhoc_cases`）。 */
  cases: AdhocCase[];
  /** 已填过的 truth（重新打开时回填）。 */
  value: Record<string, AdhocTruth>;
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (truths: AdhocTruth[]) => void;
}

const parseControl = (text: string): string[] =>
  text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

const signatureText = (value?: Record<string, unknown> | null): string => {
  if (!value) return '-';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

/** ad-hoc 补 truth：每个 case 收集 control（多行 SQL，一行一条）与 expect。
 * `expect=refuse` 时 control 可空（后端 `verify_adhoc` 同口径）；`answer` 必须至少一条。 */
export default function AdhocTruthModal({
  open, cases, value, submitting, onCancel, onSubmit,
}: AdhocTruthModalProps) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Record<string, DraftRow>>({});
  const caseIdsKey = cases.map((c) => c.case_id).join('|');

  useEffect(() => {
    if (!open) return;
    const next: Record<string, DraftRow> = {};
    for (const item of cases) {
      const prev = value[item.case_id];
      next[item.case_id] = {
        control: prev?.control?.length ? prev.control.join('\n') : '',
        expect: prev?.expect ?? 'answer',
      };
    }
    setDraft(next);
    // value 只用于打开时回填：cases/open 变化才重置草稿，避免输入中被覆盖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, caseIdsKey]);

  const missing = cases.filter((item) => {
    const row = draft[item.case_id];
    if (!row) return true;
    return row.expect === 'answer' && parseControl(row.control).length === 0;
  });

  const update = (caseId: string, patch: Partial<DraftRow>) => {
    setDraft((prev) => ({
      ...prev,
      [caseId]: {
        control: prev[caseId]?.control ?? '',
        expect: prev[caseId]?.expect ?? 'answer',
        ...patch,
      },
    }));
  };

  const submit = () => {
    if (missing.length) return;
    onSubmit(cases.map((item) => {
      const row = draft[item.case_id];
      return {
        case_id: item.case_id,
        control: parseControl(row?.control ?? ''),
        expect: row?.expect ?? 'answer',
      };
    }));
  };

  return (
    <Modal
      open={open}
      title={t('evolutionConsole.adhocTitle')}
      width={720}
      onCancel={onCancel}
      onOk={submit}
      okText={t('evolutionConsole.adhocSubmit')}
      confirmLoading={submitting}
      okButtonProps={{ disabled: missing.length > 0 }}
      destroyOnHidden
    >
      <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 12px' }}>
        {t('evolutionConsole.adhocHint')}
      </p>
      {missing.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('evolutionConsole.adhocNeedControl', { n: missing.length })}
        />
      )}
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {cases.map((item) => {
          const row = draft[item.case_id];
          const expect = row?.expect ?? 'answer';
          return (
            <div key={item.case_id} style={{ borderTop: '1px solid #f0f0f0', paddingTop: 12 }}>
              <Space size={6} wrap style={{ marginBottom: 6 }}>
                <Tooltip title={item.case_id}>
                  <Tag color="blue"><code>{item.case_id}</code></Tag>
                </Tooltip>
                {item.question && <b>{item.question}</b>}
              </Space>
              <details style={{ marginBottom: 8 }}>
                <summary style={{ cursor: 'pointer', color: '#8c8c8c', fontSize: 12 }}>
                  {t('evolutionConsole.adhocSignature')}
                </summary>
                <pre style={{ margin: '4px 0', fontSize: 12, whiteSpace: 'pre-wrap' }}>
                  {signatureText(item.failure_signature)}
                </pre>
              </details>
              <div style={{ marginBottom: 6 }}>
                <span style={{ marginRight: 8 }}>{t('evolutionConsole.adhocExpect')}</span>
                <Select<'answer' | 'refuse'>
                  size="small"
                  style={{ width: 180 }}
                  value={expect}
                  onChange={(next) => update(item.case_id, { expect: next })}
                  options={[
                    { value: 'answer', label: t('evolutionConsole.adhocExpectAnswer') },
                    { value: 'refuse', label: t('evolutionConsole.adhocExpectRefuse') },
                  ]}
                />
              </div>
              <Input.TextArea
                rows={4}
                value={row?.control ?? ''}
                placeholder={
                  expect === 'refuse'
                    ? t('evolutionConsole.adhocControlOptional')
                    : t('evolutionConsole.adhocControlPlaceholder')
                }
                onChange={(e) => update(item.case_id, { control: e.target.value })}
                style={{ fontFamily: 'monospace', fontSize: 12 }}
              />
            </div>
          );
        })}
      </Space>
    </Modal>
  );
}
