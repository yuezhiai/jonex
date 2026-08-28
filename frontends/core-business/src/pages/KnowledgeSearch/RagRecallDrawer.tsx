import { useTranslation } from 'react-i18next';
import { Drawer, Typography, Descriptions, Card, Empty, Space, Spin } from 'antd';

const { Title, Text } = Typography;

export interface RagRecallItem {
  doc_id: string;
  file_name: string;
  kb_id: string;
  chunk_index: number;
  chunk_id: string;
  text: string;
  score?: number;
}

interface RagRecallDrawerProps {
  open: boolean;
  loading: boolean;
  item: RagRecallItem | null;
  rank: number;
  score?: number | null;
  onClose: () => void;
}

function SectionHeader({ title }: { title: string }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        marginBottom: 16,
      }}
    >
      <div
        style={{
          width: 4,
          height: 16,
          background: '#3b82f6',
          borderRadius: 2,
        }}
      />
      <Title level={5} style={{ margin: 0, fontWeight: 600 }}>
        {title}
      </Title>
    </div>
  );
}

export default function RagRecallDrawer({ open, loading, item, rank, score, onClose }: RagRecallDrawerProps) {
  const { t } = useTranslation();

  return (
    <Drawer
      title={t('knowledgeSearch.ragRecallDetail')}
      size="large"
      open={open}
      onClose={onClose}
      destroyOnHidden
    >
      <Spin spinning={loading}>
      {item ? (
        <Space vertical size="large" style={{ width: '100%' }}>
          <section>
            <SectionHeader title={t('knowledgeSearch.recallInfo')} />
            <Descriptions
              column={2}
              layout="vertical"
              size="small"
              colon={false}
              styles={{ label: { fontWeight: 600 } }}
            >
              <Descriptions.Item label={t('knowledgeSearch.documentName')}>
                {item.file_name || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('knowledgeSearch.recallRank')}>
                #{rank}
              </Descriptions.Item>
              <Descriptions.Item label={t('knowledgeSearch.relevance')}>
                {score != null ? `${(score * 100).toFixed(2)}%` : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('knowledgeSearch.chunkId')}>
                {item.chunk_id || '—'}
              </Descriptions.Item>
            </Descriptions>
          </section>

          <section>
            <SectionHeader title={t('knowledgeSearch.fullChunkContent')} />
            <Card
              variant="borderless"
              styles={{
                body: {
                  maxHeight: 420,
                  overflow: 'auto',
                  padding: 16,
                  background: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  borderRadius: 12,
                },
              }}
            >
              <Text
                style={{
                  fontSize: 14,
                  lineHeight: 1.8,
                  color: '#334155',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {item.text || '—'}
              </Text>
            </Card>
          </section>
        </Space>
      ) : (
        <Empty description={t('knowledgeSearch.ragRecallEmpty')} />
      )}
      </Spin>
    </Drawer>
  );
}
