import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Space, Tag, Spin, Empty } from 'antd';
import { FileTextOutlined, RightOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { getDocumentChunks } from '@/api/domainKnowledge';
import MarkdownContent from '@/components/MarkdownContent';
import type { DocumentChunk } from '@/types/domainKnowledge';

function formatTimeRange(timeStart: number, timeEnd: number | null): string {
  const fmt = (sec: number): string => {
    const s = Math.max(0, Math.floor(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  };
  if (timeEnd != null && timeEnd > timeStart) {
    return `${fmt(timeStart)} - ${fmt(timeEnd)}`;
  }
  return fmt(timeStart);
}

export interface ProcessingStage {
  key: string;
  label: string;
  icon: React.ReactNode;
}

interface StageDetailCardProps {
  stage: ProcessingStage;
  docId?: string;
  refreshKey?: string;
  mediaType?: 'video' | 'document';
  onPlayVideo?: (timeStart?: number | null) => void;
}

export default function StageDetailCard({ stage, docId, refreshKey, mediaType, onPlayVideo }: StageDetailCardProps) {
  const { t } = useTranslation();
  const [chunks, setChunks] = useState<DocumentChunk[]>([]);
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(null);
  const [chunksLoading, setChunksLoading] = useState(false);

  const selectedChunk = useMemo(
    () => chunks.find((c) => c.chunk_id === selectedChunkId) ?? chunks[0],
    [chunks, selectedChunkId],
  );

  const selectedIndex = useMemo(
    () => (selectedChunk ? chunks.findIndex((c) => c.chunk_id === selectedChunk.chunk_id) : -1),
    [chunks, selectedChunk],
  );

  useEffect(() => {
    if (stage.key === 'parse' && docId) {
      let cancelled = false;
      setChunksLoading(true);
      getDocumentChunks(docId)
        .then((items) => {
          if (cancelled) return;
          setChunks(items);
          setSelectedChunkId((current) => items.some((c) => c.chunk_id === current) ? current : items[0]?.chunk_id ?? null);
        })
        .catch(() => {})
        .finally(() => { if (!cancelled) setChunksLoading(false); });
      return () => { cancelled = true; };
    }
  }, [stage.key, docId, refreshKey]);

  if (stage.key !== 'parse') return null;

  // 首层卡面与内边距由 PageContent 承担（这里不再套 Card / Space，否则是「卡中卡」）；
  // 高度也不再写死 calc(100vh - Npx) 去猜剩余空间，改由 pane 的 flex 撑满。
  // 文档分段：标题与面板外框都去掉——这页只有分段一项，标题是废话；
  // 外框的边框圆角与 PageContent 的卡片重复，左右分隔由列表自带的 borderRight 承担。
  return chunksLoading ? (
    <div style={{ textAlign: 'center', padding: 24 }}>
      <Spin tip={t('common.loading')} />
    </div>
  ) : chunks?.length > 0 ? (
    <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
      {/* 左侧片段列表：不铺底色，避免与卡片白底叠出第二层"面板"观感，靠 borderRight 分隔 */}
      <div
        style={{
          width: 220,
          padding: '16px 12px',
          borderRight: '1px solid #f1f5f9',
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: '#0b2b5c',
            marginBottom: 12,
            flexShrink: 0,
          }}
        >
          {t('domainKnowledge.originalText')}
        </div>
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {chunks.map((chunk, idx) => {
              const active = selectedChunk?.chunk_id === chunk.chunk_id;
              return (
                <div
                  key={chunk.chunk_id}
                  onClick={() => setSelectedChunkId(chunk.chunk_id)}
                  style={{
                    padding: '10px 12px',
                    borderRadius: 8,
                    cursor: 'pointer',
                    background: active ? '#fff' : 'transparent',
                    border: `1px solid ${active ? '#e2e8f0' : 'transparent'}`,
                    fontSize: 13,
                    color: active ? '#3b82f6' : '#475569',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                  }}
                >
                  <span>
                    {t('domainKnowledge.chunkLabel', {
                      index: idx + 1,
                    })}
                  </span>
                  {active && <RightOutlined style={{ fontSize: 12 }} />}
                </div>
              );
            })}
          </Space>
        </div>
      </div>

      {/* 右侧内容区 */}
      <div
        style={{
          flex: 1,
          padding: 24,
          minWidth: 0,
          overflowY: 'auto',
        }}
      >
        {selectedChunk && (
          <div style={{ maxWidth: 960, margin: '0 auto' }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'flex-start',
                marginBottom: 20,
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: 16,
                    fontWeight: 600,
                    color: '#0b2b5c',
                    marginBottom: 10,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                  }}
                >
                  <FileTextOutlined style={{ color: '#3b82f6' }} />
                  <span
                    style={{
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {getChunkTitle(selectedChunk) || t('domainKnowledge.chunkLabel', { index: selectedIndex + 1 })}
                  </span>
                </div>
                <Tag
                  style={{
                    border: 'none',
                    borderRadius: 6,
                    background: '#f1f5f9',
                    color: '#64748b',
                    fontSize: 12,
                  }}
                >
                  {t('domainKnowledge.chunkInfo', {
                    index: selectedIndex + 1,
                    total: chunks.length,
                    length: selectedChunk.content_length,
                  })}
                </Tag>
                {mediaType === 'video' && (
                  <Button
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    onClick={() => onPlayVideo?.(selectedChunk.time_start)}
                    style={{ borderRadius: 8, marginLeft: 20 }}
                  >
                    {t('domainKnowledge.playVideo')}
                  </Button>
                )}
              </div>
            </div>
            <div className="yx-doc-passage" style={{ marginBottom: 12 }}>
              <MarkdownContent content={selectedChunk.content ?? selectedChunk.content_summary ?? ''} />
            </div>
          </div>
        )}
      </div>
    </div>
  ) : (
    <Empty description={t('domainKnowledge.noChunkData')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
  );
}

function getChunkTitle(chunk: DocumentChunk): string {
  const firstHeading = (chunk.content ?? chunk.content_summary)?.match(/^#{1,6}\s+(.+)$/m)?.[1];
  if (firstHeading) return firstHeading.replace(/\\/g, '');
  const fileName = chunk.file_name || chunk.file_path?.match(/file=([^|]+)/)?.[1];
  if (fileName) {
    const cleanName = fileName
      .replace(/\.[^.|]+$/u, '')
      .replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_/gi, '');
    return cleanName.replace(/_+/g, ' ').trim() || 'Unnamed Document';
  }
  return '';
}
