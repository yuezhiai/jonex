// 新建知识库工具的 AI 草稿：输入描述 / curl / OpenAPI，生成后预览并回填表单。
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Input, Modal, Space, Tag, Typography, message } from 'antd';
import { proposeKbToolDraft, type KbToolAiDraft, type KbToolAiProposal } from '@/api/kbTool';

const { TextArea } = Input;

interface Props {
  open: boolean;
  kbId: string;
  onClose: () => void;
  onApply: (draft: KbToolAiDraft) => void;
}

export default function KbToolAiDraftModal({ open, kbId, onClose, onApply }: Props) {
  const { t } = useTranslation();
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(false);
  const [proposal, setProposal] = useState<KbToolAiProposal | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setText('');
    setLoading(false);
    setProposal(null);
    setError(null);
  }, [open]);

  const generate = async () => {
    if (!text.trim()) {
      message.warning(t('kbTool.ai.inputRequired'));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await proposeKbToolDraft(kbId, text.trim());
      setProposal(res);
      if (res.error) setError(res.error);
    } catch (e: any) {
      setError(e?.message || t('kbTool.ai.failed'));
    } finally {
      setLoading(false);
    }
  };

  const draft = proposal?.draft || null;
  const runtime = draft?.runtime;
  const authType = runtime?.auth?.type || 'none';

  return (
    <Modal
      title={t('kbTool.ai.title')}
      open={open}
      onCancel={onClose}
      width={720}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={onClose}>{t('common.cancel')}</Button>,
        <Button key="generate" loading={loading} onClick={() => void generate()}>
          {t('kbTool.ai.generate')}
        </Button>,
        <Button
          key="apply"
          type="primary"
          disabled={!draft}
          onClick={() => {
            if (draft) {
              onApply(draft);
              onClose();
            }
          }}
        >
          {t('kbTool.ai.apply')}
        </Button>,
      ]}
    >
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
        {t('kbTool.ai.hint')}
      </Typography.Paragraph>
      <TextArea
        rows={7}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t('kbTool.ai.placeholder')}
        style={{ fontFamily: 'monospace', fontSize: 12 }}
      />
      {error && <Alert type="error" showIcon style={{ marginTop: 12 }} title={error} />}
      {!!proposal?.warnings?.length && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          title={<ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {proposal.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>}
        />
      )}
      {draft && (
        <div style={{ marginTop: 12, padding: 12, border: '1px solid #e5e7eb', borderRadius: 8 }}>
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color="blue">{draft.toolName}</Tag>
              <Typography.Text strong>{draft.name}</Typography.Text>
            </Space>
            {draft.description && (
              <Typography.Text type="secondary">{draft.description}</Typography.Text>
            )}
            <Typography.Text>
              {t('kbTool.ai.runtime')}：
              <code style={{ marginInlineStart: 6 }}>
                {runtime?.method || 'POST'} {runtime?.url || t('kbTool.ai.urlMissing')}
              </code>
            </Typography.Text>
            <Typography.Text>
              {t('kbTool.ai.auth')}：<Tag>{authType}</Tag>
            </Typography.Text>
            {draft.readOnlySuggestion && (
              <Alert
                type={draft.readOnlySuggestion.value ? 'info' : 'warning'}
                showIcon
                title={t('kbTool.ai.readOnlySuggestion', {
                  value: draft.readOnlySuggestion.value
                    ? t('kbTool.ai.readOnlyYes')
                    : t('kbTool.ai.readOnlyNo'),
                  reason: draft.readOnlySuggestion.reason || '—',
                })}
              />
            )}
            {!!draft.assumptions.length && (
              <div>
                <Typography.Text type="secondary">{t('kbTool.ai.assumptions')}</Typography.Text>
                <ul style={{ margin: '4px 0 0', paddingInlineStart: 18 }}>
                  {draft.assumptions.map((a) => <li key={a}>{a}</li>)}
                </ul>
              </div>
            )}
            {!!draft.unsupported.length && (
              <div>
                <Typography.Text type="secondary">{t('kbTool.ai.unsupported')}</Typography.Text>
                <ul style={{ margin: '4px 0 0', paddingInlineStart: 18 }}>
                  {draft.unsupported.map((a) => <li key={a}>{a}</li>)}
                </ul>
              </div>
            )}
          </Space>
        </div>
      )}
    </Modal>
  );
}
