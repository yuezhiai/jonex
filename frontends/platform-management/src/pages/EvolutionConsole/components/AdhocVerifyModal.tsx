import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, Modal, Radio, Space, Tag, Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import { fetchAdhocEvidence } from '../../../api/evolution';
import type { AdhocCase, AdhocEvidence, AdhocVerdict } from '../../../api/evolution';

interface VerdictDraft {
  verdict?: 'pass' | 'fail';
  note: string;
}

interface EvidenceState {
  loading: boolean;
  data?: AdhocEvidence;
  /** 请求本身失败（网络/4xx/5xx）；业务性空结果走 `data.error`。 */
  error?: string;
}

interface AdhocVerifyModalProps {
  open: boolean;
  /** 发起验真的 run（取数接口是 `POST /runs/{run_id}/adhoc-evidence`）。 */
  runId: string;
  /** 预览返回的待验真 case（`AdoptPreview.adhoc_cases`）。 */
  cases: AdhocCase[];
  /** 本次采纳勾选的条目（后端据此临时 apply 候选）。 */
  picks: string[];
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (verdicts: AdhocVerdict[]) => void;
}

const errorMessage = (err: unknown): string => {
  if (err instanceof Error) return err.message;
  return String(err);
};

const cellText = (cell: unknown): string => {
  if (cell == null) return '';
  if (typeof cell === 'object') {
    try {
      return JSON.stringify(cell);
    } catch {
      return String(cell);
    }
  }
  return String(cell);
};

/** 行可能是数组（② 的常规形状），也可能是对象（按列名取值）；两种都渲染。 */
const rowCells = (row: unknown, columns: string[]): unknown[] => {
  if (Array.isArray(row)) return row;
  if (row && typeof row === 'object') {
    const record = row as Record<string, unknown>;
    return columns.map((column) => record[column]);
  }
  return [row];
};

const cellStyle: React.CSSProperties = {
  border: '1px solid #f0f0f0',
  padding: '4px 8px',
  textAlign: 'left',
  whiteSpace: 'nowrap',
};

/** alias / synonym 批的人工验真卡：每个 case 先用本次勾选的候选跑一次问句
 * （alias 是临时 apply schema，synonym 是请求级值同义词），看到数据后再判
 * 「通过 / 不通过」。`pass` 必须拿到 `evidence_id` 才能选（票据绑定本次
 * picks 的条目，勾选变化后旧证据失效）；`fail` 可填理由。全部 case 判定后才能提交。 */
export default function AdhocVerifyModal({
  open, runId, cases, picks, submitting, onCancel, onSubmit,
}: AdhocVerifyModalProps) {
  const { t } = useTranslation();
  const [evidence, setEvidence] = useState<Record<string, EvidenceState>>({});
  const [drafts, setDrafts] = useState<Record<string, VerdictDraft>>({});
  // 请求序号：全局单调递增，reset 只清映射不回收序号 —— 老响应回来时
  // 一定对不上新序号（同序号复用会让刚重置后的旧 ticket 混进来）。
  const seqRef = useRef(0);
  const tokens = useRef<Record<string, number>>({});
  const caseIdsKey = cases.map((item) => item.case_id).join('|');
  const picksKey = picks.join('|');

  useEffect(() => {
    if (!open) return;
    // 每次打开 / 换 case / 换勾选都重置：旧证据的票据不再对被采纳的条目有效。
    tokens.current = {};
    setEvidence({});
    setDrafts({});
  }, [open, caseIdsKey, picksKey]);

  const loadEvidence = async (caseId: string) => {
    const token = ++seqRef.current;
    tokens.current[caseId] = token;
    const isStale = () => tokens.current[caseId] !== token;
    setEvidence((prev) => ({ ...prev, [caseId]: { loading: true } }));
    try {
      const data = await fetchAdhocEvidence(runId, caseId, picks);
      if (isStale()) return;
      setEvidence((prev) => ({ ...prev, [caseId]: { loading: false, data } }));
      if (!data.evidence_id) {
        // 执行异常 / 恢复失败：没有票据就没有判定入口（只能重试）。
        setDrafts((prev) => ({
          ...prev,
          [caseId]: { verdict: undefined, note: prev[caseId]?.note ?? '' },
        }));
      }
    } catch (err) {
      if (isStale()) return;
      setEvidence((prev) => ({
        ...prev,
        [caseId]: { loading: false, error: errorMessage(err) },
      }));
      // 证据没了就不能保留旧判定（尤其不能保留 pass 的票据）。
      setDrafts((prev) => ({
        ...prev,
        [caseId]: { verdict: undefined, note: prev[caseId]?.note ?? '' },
      }));
    }
  };

  const setVerdict = (caseId: string, patch: Partial<VerdictDraft>) => {
    setDrafts((prev) => ({
      ...prev,
      [caseId]: {
        verdict: prev[caseId]?.verdict,
        note: prev[caseId]?.note ?? '',
        ...patch,
      },
    }));
  };

  const evidenceIdOf = (caseId: string): string | null =>
    evidence[caseId]?.data?.evidence_id ?? null;

  /** 判定要带「当时看到的数据摘要」（行数 + 内容指纹），不落完整 rows。 */
  const evidenceSummaryOf = (caseId: string): { row_count?: number; rows_hash?: string } => {
    const data = evidence[caseId]?.data;
    if (!data) return {};
    return { row_count: data.row_count, rows_hash: data.rows_hash };
  };

  /** 拿到「有票据的返回」或还没取数时才可判定：
   * - 请求失败（网络/4xx/5xx）→ 只能重试；
   * - 后端执行异常 / 恢复失败（返回了 payload 但 `evidence_id` 为空）→ 只能重试；
   * - 空结果 / 拒答照常带票据（`ok=false` 但 `evidence_id` 非空）→ 仍可判通过/不通过。 */
  const isJudgeable = (caseId: string): boolean => {
    const state = evidence[caseId];
    if (state?.error) return false;
    const data = state?.data;
    if (data && !data.evidence_id) return false;
    return true;
  };

  const isJudged = (caseId: string): boolean => {
    if (!isJudgeable(caseId)) return false;
    const draft = drafts[caseId];
    if (draft?.verdict === 'fail') return true;
    return draft?.verdict === 'pass' && Boolean(evidenceIdOf(caseId));
  };

  const pendingCount = cases.filter((item) => !isJudged(item.case_id)).length;
  const canSubmit = cases.length > 0 && pendingCount === 0;

  const submit = () => {
    if (!canSubmit) return;
    const verdicts: AdhocVerdict[] = [];
    for (const item of cases) {
      if (!isJudgeable(item.case_id)) return;
      const draft = drafts[item.case_id];
      if (draft?.verdict === 'pass') {
        const evidenceId = evidenceIdOf(item.case_id);
        if (!evidenceId) return;
        verdicts.push({
          case_id: item.case_id,
          verdict: 'pass',
          evidence_id: evidenceId,
          ...evidenceSummaryOf(item.case_id),
        });
      } else if (draft?.verdict === 'fail') {
        const note = draft.note.trim();
        verdicts.push({
          case_id: item.case_id,
          verdict: 'fail',
          ...(note ? { note } : {}),
          ...evidenceSummaryOf(item.case_id),
        });
      } else {
        return;
      }
    }
    onSubmit(verdicts);
  };

  return (
    <Modal
      open={open}
      title={t('evolutionConsole.adhocVerifyTitle')}
      width={880}
      onCancel={onCancel}
      onOk={submit}
      okText={t('evolutionConsole.adhocVerifySubmit')}
      confirmLoading={submitting}
      okButtonProps={{ disabled: !canSubmit }}
      destroyOnHidden
    >
      <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 12px' }}>
        {t('evolutionConsole.adhocVerifyHint')}
      </p>
      {!canSubmit && cases.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('evolutionConsole.adhocVerifyNeedAll', { n: pendingCount })}
        />
      )}
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {cases.map((item) => {
          const state = evidence[item.case_id];
          const data = state?.data;
          const draft = drafts[item.case_id];
          const verdict = draft?.verdict;
          const judgeable = isJudgeable(item.case_id);
          // synonym 批的证据带 `term` / `canonical`（值同义词），alias 批带
          // `alias` / `concept`（概念别名）。两组键按 kind 二选一，优先认 synonym。
          const synonymCard = Boolean(data?.term || data?.canonical);
          return (
            <div key={item.case_id} style={{ borderTop: '1px solid #f0f0f0', paddingTop: 12 }}>
              <Space size={6} wrap style={{ marginBottom: 8 }}>
                <Tooltip title={item.case_id}>
                  <Tag color="blue"><code>{item.case_id}</code></Tag>
                </Tooltip>
                {item.question && <b>{item.question}</b>}
              </Space>

              <div style={{ marginBottom: 8 }}>
                <Button
                  size="small"
                  loading={state?.loading}
                  onClick={() => loadEvidence(item.case_id)}
                >
                  {t('evolutionConsole.adhocVerifyViewData')}
                </Button>
                {state?.error && (
                  <span style={{ color: '#cf1322', marginLeft: 8, fontSize: 12 }}>
                    {t('evolutionConsole.adhocVerifyFetchFailed', { msg: state.error })}
                  </span>
                )}
              </div>

              {data && (
                <div style={{ marginBottom: 8 }}>
                  <Space size={6} wrap style={{ marginBottom: 6 }}>
                    {synonymCard ? (
                      <>
                        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                          {t('evolutionConsole.adhocVerifyTerm')}
                        </span>
                        <code>
                          {data.terms && data.terms.length > 0
                            ? data.terms.join(' / ')
                            : (data.term || '-')}
                        </code>
                        <span style={{ color: '#8c8c8c' }}>→</span>
                        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                          {t('evolutionConsole.adhocVerifyCanonical')}
                        </span>
                        <b>{data.canonical || '-'}</b>
                      </>
                    ) : (
                      <>
                        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                          {t('evolutionConsole.adhocVerifyAlias')}
                        </span>
                        <code>{data.alias || '-'}</code>
                        <span style={{ color: '#8c8c8c' }}>→</span>
                        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                          {t('evolutionConsole.adhocVerifyConcept')}
                        </span>
                        <b>{data.concept || '-'}</b>
                        {(data.concept_aliases ?? []).map((alias) => (
                          <Tag key={alias}>{alias}</Tag>
                        ))}
                      </>
                    )}
                  </Space>
                  {data.error && (
                    <Alert
                      type={data.ok ? 'info' : 'warning'}
                      showIcon
                      style={{ marginBottom: 6 }}
                      message={data.error}
                    />
                  )}
                  <div style={{ color: '#8c8c8c', fontSize: 12, marginBottom: 4 }}>
                    {t('evolutionConsole.adhocVerifyRowCount', { n: data.row_count })}
                  </div>
                  {data.columns.length > 0 && data.rows.length > 0 && (
                    <div style={{ overflowX: 'auto' }}>
                      <table
                        style={{
                          width: '100%', borderCollapse: 'collapse', fontSize: 12,
                        }}
                      >
                        <thead>
                          <tr>
                            {data.columns.map((column) => (
                              <th key={column} style={{ ...cellStyle, background: '#fafafa' }}>
                                {column}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {data.rows.map((row, rowIndex) => (
                            <tr key={rowIndex}>
                              {rowCells(row, data.columns).map((cell, cellIndex) => (
                                <td key={cellIndex} style={cellStyle}>{cellText(cell)}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {data.ok && data.row_count === 0 && (
                    <div style={{ color: '#8c8c8c', fontSize: 12 }}>
                      {t('evolutionConsole.adhocVerifyNoRows')}
                    </div>
                  )}
                </div>
              )}

              <div style={{ marginBottom: verdict === 'fail' ? 8 : 0 }}>
                <Radio.Group
                  value={verdict}
                  onChange={(event) => setVerdict(item.case_id, { verdict: event.target.value })}
                >
                  <Radio
                    value="pass"
                    disabled={!judgeable || !evidenceIdOf(item.case_id)}
                  >
                    {t('evolutionConsole.adhocVerifyPass')}
                  </Radio>
                  <Radio value="fail" disabled={!judgeable}>
                    {t('evolutionConsole.adhocVerifyFail')}
                  </Radio>
                </Radio.Group>
              </div>
              {verdict === 'fail' && judgeable && (
                <Input.TextArea
                  rows={2}
                  value={draft?.note ?? ''}
                  placeholder={t('evolutionConsole.adhocVerifyNotePlaceholder')}
                  onChange={(event) => setVerdict(item.case_id, { note: event.target.value })}
                />
              )}
            </div>
          );
        })}
      </Space>
    </Modal>
  );
}
