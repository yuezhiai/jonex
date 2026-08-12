import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button, Space, Tag } from 'antd';
import {
  ArrowLeftOutlined,
  FileTextOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  BuildOutlined,
} from '@ant-design/icons';
import type { DomainKnowledgeDetail, DocumentStatsResult } from '@/types/domainKnowledge';
import { getDomainKnowledgeDetail, getDocumentStats } from '@/api/domainKnowledge';
import { listDataSources } from '@/api/dataSource';
import { dataSourceInstanceDisplayName } from '@/utils/dataSourceDisplay';
import DocumentLibrary from './DocumentLibrary';

export default function DomainKnowledgeBlank() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id = '' } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<DomainKnowledgeDetail | null>(null);
  const [dataSources, setDataSources] = useState<string>('—');
  const [docStats, setDocStats] = useState<DocumentStatsResult | null>(null);

  useEffect(() => {
    if (!id) return;
    getDomainKnowledgeDetail(id).then(setDetail);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    getDocumentStats(id)
      .then(setDocStats)
      .catch(() => setDocStats(null));
  }, [id]);

  useEffect(() => {
    if (!id) return;
    listDataSources(id)
      .then((list) => setDataSources(list.map((ds) => dataSourceInstanceDisplayName(ds, t)).join(' · ') || '—'))
      .catch(() => setDataSources('—'));
  }, [id, t]);

  // 互斥四桶口径（与统计栏、后端 documents_stats 同源）：处理中+已完成+编译失败+解析失败=总计
  // 失败类指标常驻显示（即使为 0）。
  const stats: {
    labelKey: string;
    value: string;
    icon: JSX.Element;
    suffixKey?: string;
  }[] = [
    {
      labelKey: 'docPhase.total',
      value: (docStats?.total ?? detail?.documentCount ?? 0).toLocaleString(),
      suffixKey: 'docPhase.docSuffix',
      icon: <FileTextOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      labelKey: 'docPhase.processing',
      value: docStats ? docStats.processing.toLocaleString() : '—',
      icon: <ClockCircleOutlined style={{ color: '#3b82f6' }} />,
    },
    {
      labelKey: 'docPhase.completedStat',
      value: docStats ? docStats.completed.toLocaleString() : '—',
      icon: <CheckCircleOutlined style={{ color: '#22c55e' }} />,
    },
    {
      labelKey: 'docPhase.compileFailedStat',
      value: docStats ? docStats.compileFailed.toLocaleString() : '—',
      icon: <BuildOutlined style={{ color: '#f59e0b' }} />,
    },
    {
      labelKey: 'docPhase.parseFailedStat',
      value: docStats ? docStats.parseFailed.toLocaleString() : '—',
      icon: <CloseCircleOutlined style={{ color: '#ef4444' }} />,
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <div style={{ flexShrink: 0 }}>
        <Space align="center" size={12}>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/domain-knowledge')}>
            {t('common.back')}
          </Button>
          <Space align="center" size={10}>
            <FileTextOutlined style={{ fontSize: 24, color: '#3b82f6' }} />
            <h1
              style={{
                margin: 0,
                fontSize: 22,
                color: '#0b2b5c',
                fontWeight: 600,
              }}
            >
              {detail?.name}
            </h1>
          </Space>
          <Tag
            icon={<DatabaseOutlined />}
            style={{
              fontSize: 14,
              padding: '4px 12px',
              borderRadius: 8,
              color: '#64748b',
              borderColor: '#e2e8f0',
            }}
          >
            {detail?.spaceName}
          </Tag>
        </Space>
      </div>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 24,
          marginTop: 24,
          flexShrink: 0,
        }}
      >
        <Space align="center" size={24}>
          {stats.map((s) => (
            <Space key={s.labelKey} align="center" size={8}>
              {s.icon}
              <span style={{ color: '#64748b', fontSize: 14 }}>{t(s.labelKey)}</span>
              <span style={{ color: '#0b2b5c', fontSize: 18, fontWeight: 700 }}>{s.value}</span>
              {s.suffixKey && <span style={{ color: '#64748b', fontSize: 14 }}>{t(s.suffixKey)}</span>}
            </Space>
          ))}
        </Space>
        <Space align="center" size={6} style={{ color: '#64748b', fontSize: 14 }}>
          <CloudServerOutlined style={{ color: '#8b5cf6' }} />
          <span>
            {t('dataSource.title')}: {dataSources}
          </span>
        </Space>
      </div>

      <div style={{ flex: 1, minHeight: 0 }}>
        <DocumentLibrary kbId={id} kbType={detail?.kbType} />
      </div>
    </div>
  );
}
