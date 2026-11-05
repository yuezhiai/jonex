import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Input, Modal, Space, Spin, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import { adjustEntry, editEntry, type PatchEntry } from '../../api/evolution';
import SqlPre from './components/SqlPre';

interface ChatTurn {
  role: 'user' | 'assistant';
  /** user：原话；assistant：修改理由（修订稿在 revised 里）。 */
  text: string;
  /** assistant 独有：该轮产出的修订稿。 */
  revised?: string;
}

interface Props {
  open: boolean;
  runId: string;
  entry: PatchEntry;
  onClose: () => void;
  /** 应用修订成功后回调（RunDetail 里 = 重新 load()）。 */
  onApplied: () => void;
}

/** 条目「人工调整」：对话一轮产修订稿 → 「应用修订」写回 patch.json（第二层 b，2026-10-12）。
 * 多轮状态只在本组件（关窗即丢）；alias 条目应用后 key 会变、query_rule 的 key 不变。 */
export default function EntryAdjustModal({ open, runId, entry, onClose, onApplied }: Props) {
  const { t } = useTranslation();
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorTail, setErrorTail] = useState('');
  /** 会话号：每次打开 +1，同时充当在途响应的序号守卫（先例 StartRunModal 的 loadSeqRef）。 */
  const sessionRef = useRef(0);

  // 关窗即丢：每个打开周期都是新会话，上一周期的对话与迟到响应都不再认。
  // antd v6 的 destroyOnHidden 只销毁 Modal 子树、清不掉本组件 state，须显式重置
  // （同 StartRunModal「重开 resetFields」的先例）；不重置时，上一个条目的对话与修订稿
  // 会残留到下一个条目的弹窗里，「应用修订」就可能把 A 的修订写进 B 的 entry_key。
  useEffect(() => {
    if (!open) return;
    sessionRef.current += 1;
    setTurns([]);
    setDraft('');
    setSending(false);
    setApplying(false);
    setError(null);
    setErrorTail('');
  }, [open]);

  const isAlias = entry.concept !== undefined;
  const current = isAlias ? (entry.alias ?? '') : (entry.expression ?? '');
  // 与后端 `entry_key` **逐字同口径**：constraint 等带 `op` 的条目身份是 `name`
  // （add 在规划后带生成名）；普通 query_rule 用 `target_code`。用 target_code
  // 去找 constraint 会被 `_find_entry` 判成「条目不在本批」1001。
  const entryKey = isAlias
    ? `${entry.concept}:${entry.alias}`
    : entry.op !== undefined
      ? String(entry.name ?? entry.target_code ?? '')
      : String(entry.target_code ?? '');
  const latest = useMemo(
    () => [...turns].reverse().find((x) => x.revised)?.revised ?? '', [turns]);

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    const session = sessionRef.current; // 序号守卫：响应落地前先确认还在同一会话
    const next = [...turns, { role: 'user' as const, text }];
    setTurns(next);
    setDraft('');
    setSending(true);
    setError(null);
    setErrorTail('');
    try {
      const res = await adjustEntry(runId, {
        entry_key: entryKey,
        messages: next.map((x) => ({
          role: x.role,
          content: x.revised
            ? t('evolutionConsole.adjustAssistantContent', { revised: x.revised, rationale: x.text })
            : x.text,
        })),
      });
      if (session !== sessionRef.current) return; // 期间关窗/重开：迟到响应丢弃
      if (res.ok && res.revised) {
        setTurns([...next, { role: 'assistant', text: res.rationale ?? '', revised: res.revised }]);
      } else {
        setError(res.timeout
          ? t('evolutionConsole.adjustTimeout')
          : t('evolutionConsole.adjustFailed', { rc: res.rc }));
        setErrorTail(`${res.stderr_tail ?? ''}\n${res.stdout_tail ?? ''}`.trim());
      }
    } catch (e: unknown) {
      if (session !== sessionRef.current) return;
      setError(e instanceof Error ? e.message : t('common.error'));
    } finally {
      if (session === sessionRef.current) setSending(false);
    }
  };

  const apply = async () => {
    if (!latest || applying) return;
    const session = sessionRef.current;
    setApplying(true);
    setError(null);
    setErrorTail('');
    try {
      const res = await editEntry(runId, isAlias
        ? { entry_key: entryKey, alias: latest }
        : { entry_key: entryKey, expression: latest });
      if (res.ok) {
        onApplied(); // 写盘已发生：无论会话是否切换都要刷新（否则停在旧 bundle，用户以为没生效再点一次）
        if (session === sessionRef.current) onClose(); // 关窗/重开过就别关新一轮的窗
        return;
      }
      if (session !== sessionRef.current) return; // 期间关窗/重开：失败详情不落旧会话
      setError(t('evolutionConsole.adjustApplyFailed', { rc: res.rc }));
      setErrorTail(`${res.stderr_tail ?? ''}\n${res.stdout_tail ?? ''}`.trim());
    } catch (e: unknown) {
      if (session !== sessionRef.current) return;
      setError(e instanceof Error ? e.message : t('common.error'));
    } finally {
      if (session === sessionRef.current) setApplying(false);
    }
  };

  return (
    <Modal open={open} title={t('evolutionConsole.adjustTitle', { key: entryKey })} width={760}
           onCancel={onClose} footer={null} destroyOnHidden>
      <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
        {t('evolutionConsole.adjustTargetLocked')}
        {/* 分隔用全角空格，转义写法（裸字符会触 no-irregular-whitespace；先例 LedgersTab） */}
        {entry.question && <span>{'\u3000'}「{entry.question}」</span>}
        {entry.raw_description && <span>{'\u3000'}{entry.raw_description}</span>}
      </p>
      <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
        <div style={{ flex: 1 }}>
          <b>{t('evolutionConsole.adjustOriginal')}</b>
          <SqlPre text={current} maxHeight={160} />
        </div>
        <div style={{ flex: 1 }}>
          <b>{t('evolutionConsole.adjustRevised')}</b>
          {latest
            ? <SqlPre text={latest} maxHeight={160} />
            : <div style={{ color: '#8c8c8c', fontSize: 12, paddingTop: 8 }}>
                {t('evolutionConsole.adjustEmptyGuide')}
              </div>}
        </div>
      </div>
      <div style={{ maxHeight: 260, overflow: 'auto', marginBottom: 8 }}>
        {turns.map((x, i) => (
          <div key={i} style={{ textAlign: x.role === 'user' ? 'right' : 'left', marginBottom: 6 }}>
            <Tag color={x.role === 'user' ? 'blue' : 'green'}>
              {x.role === 'user' ? t('evolutionConsole.adjustYou') : t('evolutionConsole.adjustModel')}
            </Tag>
            <span>{x.text || (x.revised ? `→ ${x.revised}` : '')}</span>
          </div>
        ))}
        {sending && <Spin size="small" />}
      </div>
      {error && (
        <Alert type="error" showIcon style={{ marginBottom: 8 }} message={error}
               description={errorTail ? <SqlPre text={errorTail} maxHeight={160} /> : undefined} />
      )}
      <Input.TextArea value={draft} onChange={(e) => setDraft(e.target.value)}
                      autoSize={{ minRows: 2, maxRows: 4 }}
                      placeholder={t('evolutionConsole.adjustPlaceholder')}
                      onPressEnter={(e) => {
                        if (!e.shiftKey) { e.preventDefault(); void send(); }
                      }} />
      <div style={{ marginTop: 8, textAlign: 'right' }}>
        <Space>
          <Button onClick={() => void send()} loading={sending} disabled={!draft.trim()}>
            {t('evolutionConsole.adjustSend')}
          </Button>
          <Button type="primary" onClick={() => void apply()} loading={applying} disabled={!latest}>
            {t('evolutionConsole.adjustApply')}
          </Button>
        </Space>
      </div>
    </Modal>
  );
}
