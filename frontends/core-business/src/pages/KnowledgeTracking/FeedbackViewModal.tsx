import React from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Button } from 'antd';
import type { AnswerFeedbackListItem } from '@/types/knowledgeSearch';

function formatTime(iso: string | null) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return iso;
  }
}

interface FeedbackViewModalProps {
  open: boolean;
  item: AnswerFeedbackListItem | null;
  onClose: () => void;
}

export default function FeedbackViewModal({ open, item, onClose }: FeedbackViewModalProps) {
  const { t } = useTranslation();

  return (
    <Modal
      title={<span style={{ fontSize: 16, fontWeight: 600, color: '#0b2b5c' }}>{t('tracking.feedbackDetail')}</span>}
      open={open}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t('common.close')}</Button>}
      width={600}
    >
      {item && (
        <div>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, color: '#0b2b5c', marginBottom: 6 }}>
              {t('compile.feedback.question')}
            </label>
            <div
              style={{
                padding: '12px 14px',
                background: '#f8fafc',
                border: '1px solid #eef2f6',
                borderRadius: 8,
                fontSize: 14,
                color: '#0b2b5c',
                lineHeight: 1.6,
              }}
            >
              {item.query}
            </div>
          </div>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 500, color: '#0b2b5c', marginBottom: 6 }}>
              {t('tracking.columnAnswer')}
            </label>
            <div
              style={{
                padding: '12px 14px',
                background: '#f8fafc',
                border: '1px solid #eef2f6',
                borderRadius: 8,
                fontSize: 14,
                color: '#475569',
                lineHeight: 1.6,
                maxHeight: 300,
                overflow: 'auto',
              }}
            >
              {item.answer || t('tracking.noAnswerPreview')}
            </div>
          </div>
          {item.feedback_type === 'dislike' && item.feedback_reason ? (
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: 13, fontWeight: 500, color: '#0b2b5c', marginBottom: 6 }}>
                {t('knowledgeSearch.feedbackReasonLabel')}
              </label>
              <div style={{ fontSize: 14, color: '#475569', lineHeight: 1.6 }}>
                {t(`knowledgeSearch.feedbackReason_${item.feedback_reason}`)}
              </div>
            </div>
          ) : null}
          {item.feedback_comment ? (
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: 13, fontWeight: 500, color: '#0b2b5c', marginBottom: 6 }}>
                {t('knowledgeSearch.feedbackCommentLabel')}
              </label>
              <div
                style={{
                  padding: '12px 14px',
                  background: '#f8fafc',
                  border: '1px solid #eef2f6',
                  borderRadius: 8,
                  fontSize: 14,
                  color: '#475569',
                  lineHeight: 1.6,
                }}
              >
                {item.feedback_comment}
              </div>
            </div>
          ) : null}
          <div style={{ display: 'flex', gap: 20, fontSize: 13, color: '#94a3b8' }}>
            <span>
              {t('tracking.searchTime')}
              {formatTime(item.created_at)}
            </span>
            <span>
              {t('tracking.feedbackType')}
              {item.feedback_type === 'like' ? t('knowledgeSearch.helpful') : t('knowledgeSearch.unhelpful')}
            </span>
            <span>
              {t('tracking.status')}
              {item.adopted ? t('compile.adopted') : t('tracking.notAdopted')}
            </span>
          </div>
        </div>
      )}
    </Modal>
  );
}
