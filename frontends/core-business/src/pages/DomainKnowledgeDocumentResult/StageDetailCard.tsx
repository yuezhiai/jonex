import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Space, Tag, Spin, Empty } from 'antd';
import { FileTextOutlined, RightOutlined, PlayCircleOutlined } from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import { getDocumentChunks } from '@/api/domainKnowledge';
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
  mediaType?: 'video' | 'document';
  onPlayVideo?: (timeStart?: number | null) => void;
}

export default function StageDetailCard({ stage, docId, mediaType, onPlayVideo }: StageDetailCardProps) {
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
      setChunksLoading(true);
      getDocumentChunks(docId)
        .then((items) => {
          setChunks(items);
          if (items.length > 0) {
            setSelectedChunkId(items[0].chunk_id);
          }
        })
        .catch(() => {})
        .finally(() => setChunksLoading(false));
    }
  }, [stage.key, docId]);

  return (
    <Card
      style={{
        borderRadius: 12,
        border: '1px solid #eef2f6',
        boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
      }}
      styles={{ body: { padding: 24 } }}
    >
      <Space direction="vertical" size={24} style={{ width: '100%' }}>
        {/* 文档分段 (chunks) */}
        {stage.key === 'parse' && (
          <div>
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: '#0b2b5c',
                marginBottom: 12,
              }}
            >
              {t('domainKnowledge.documentChunks')}
            </div>
            {chunksLoading ? (
              <div style={{ textAlign: 'center', padding: 24 }}>
                <Spin tip={t('common.loading')} />
              </div>
            ) : chunks?.length > 0 ? (
              <div
                style={{
                  display: 'flex',
                  border: '1px solid #f1f5f9',
                  borderRadius: 12,
                  overflow: 'hidden',
                  background: '#fff',
                  height: 'calc(100vh - 440px)',
                  minHeight: 200,
                }}
              >
                {/* 左侧片段列表 */}
                <div
                  style={{
                    width: 220,
                    background: '#f8fafc',
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
                    <>
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
                              {getChunkTitle(selectedChunk)}
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
                      <div
                        style={{
                          fontSize: 14,
                          color: '#334155',
                          lineHeight: 1.8,
                        }}
                      >
                        <ReactMarkdown>{selectedChunk.content_summary}</ReactMarkdown>
                      </div>
                    </>
                  )}
                </div>
              </div>
            ) : (
              <Empty description={t('domainKnowledge.noChunkData')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </div>
        )}
      </Space>
    </Card>
  );
}

function getChunkTitle(chunk: DocumentChunk): string {
  const firstHeading = chunk.content_summary?.match(/^#\s+(.+)$/m)?.[1];
  if (firstHeading) return firstHeading.replace(/\\/g, '');
  const fileName = chunk.file_path?.match(/file=([^|]+)/)?.[1];
  if (fileName) {
    const cleanName = fileName
      .replace(/\.[^.|]+$/u, '')
      .replace(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_/gi, '');
    return cleanName.replace(/_+/g, ' ').trim() || 'Unnamed Document';
  }
  return `Chunk ${chunk.chunk_id}`;
}
