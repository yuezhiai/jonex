import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Table, Tag, Row, Col } from 'antd';
import {
  SearchOutlined,
  ApartmentOutlined,
  BlockOutlined,
  BuildOutlined,
  FileTextOutlined,
  NodeIndexOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { colors, radius } from '@jonex/platform-theme/tokens';
import { MOCK_COMPILE_TASKS } from '../../data/mock';
import { useTranslation } from 'react-i18next';
import { PageContent, PageHeader } from '@jonex/shared-lib';

export default function KnowledgeCompile() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const statusConfig: Record<string, { color: string; label: string }> = {
    running: { color: 'processing', label: t('status.running') },
    completed: { color: 'success', label: t('status.completed') },
    failed: { color: 'error', label: t('status.failed') },
    pending: { color: 'default', label: t('status.pending') },
  };

  const stats = [
    {
      title: t('knowledgeCompile.totalTasks'),
      value: MOCK_COMPILE_TASKS.length,
      icon: <BuildOutlined />,
      color: colors.accent,
      bg: `${colors.accent}15`,
    },
    {
      title: t('knowledgeCompile.totalEntities'),
      value: MOCK_COMPILE_TASKS.reduce((s, t) => s + t.entityCount, 0).toLocaleString(),
      icon: <NodeIndexOutlined />,
      color: '#10b981',
      bg: 'var(--yx-success-bg)',
    },
    {
      title: t('knowledgeCompile.totalRelations'),
      value: MOCK_COMPILE_TASKS.reduce((s, t) => s + t.relationCount, 0).toLocaleString(),
      icon: <ApartmentOutlined />,
      color: '#8b5cf6',
      bg: '#f5f3ff',
    },
    {
      title: t('knowledgeCompile.totalChunks'),
      value: MOCK_COMPILE_TASKS.reduce((s, t) => s + t.chunkCount, 0).toLocaleString(),
      icon: <BlockOutlined />,
      color: '#f59e0b',
      bg: '#fffbeb',
    },
  ];

  const subPages = [
    {
      title: t('knowledgeCompile.cardSearch'),
      desc: t('knowledgeCompile.cardSearchDesc'),
      path: '/knowledge-compile/search',
      icon: <SearchOutlined />,
      color: 'var(--yx-accent)',
    },
    {
      title: t('knowledgeCompile.cardGraph'),
      desc: t('knowledgeCompile.cardGraphDesc'),
      path: '/knowledge-compile/graph',
      icon: <ApartmentOutlined />,
      color: '#8b5cf6',
    },
    {
      title: t('knowledgeCompile.cardVector'),
      desc: t('knowledgeCompile.cardVectorDesc'),
      path: '/knowledge-compile/vector',
      icon: <BlockOutlined />,
      color: '#10b981',
    },
    {
      title: t('knowledgeCompile.cardCompile'),
      desc: t('knowledgeCompile.cardCompileDesc'),
      path: '/knowledge-compile/compile',
      icon: <ThunderboltOutlined />,
      color: '#f59e0b',
    },
  ];

  const columns = [
    {
      title: t('knowledgeCompile.taskName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => (
        <a className="yx-table-action" onClick={() => navigate('/knowledge-compile/compile')}>
          {v}
        </a>
      ),
    },
    { title: t('common.type'), dataIndex: 'type', key: 'type' },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      render: (v: string) => {
        const cfg = statusConfig[v] || { color: 'default', label: v };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: t('knowledgeCompile.entityCount'),
      dataIndex: 'entityCount',
      key: 'entityCount',
      render: (v: number) => v.toLocaleString(),
    },
    {
      title: t('knowledgeCompile.relationCount'),
      dataIndex: 'relationCount',
      key: 'relationCount',
      render: (v: number) => v.toLocaleString(),
    },
    {
      title: t('knowledgeCompile.chunkCount'),
      dataIndex: 'chunkCount',
      key: 'chunkCount',
      render: (v: number) => v.toLocaleString(),
    },
    { title: t('knowledgeCompile.updatedAt'), dataIndex: 'updatedAt', key: 'updatedAt' },
  ];

  return (
    <>
      <PageHeader title={t('knowledgeCompile.title')} style={{ marginBottom: 4 }} />
      <p style={{ color: colors.textSecondary, margin: '0 0 16px', fontSize: 14 }}>
        {t('knowledgeCompile.description')}
      </p>

      <PageContent showCard={false}>
        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          {stats.map((s) => (
            <Col xs={24} sm={12} md={6} key={s.title}>
              <Card
                style={{ borderRadius: radius.card, border: `1px solid ${colors.border}` }}
                styles={{ body: { padding: '20px 24px' } }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div
                    style={{
                      width: 44,
                      height: 44,
                      borderRadius: 10,
                      background: s.bg,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 20,
                      color: s.color,
                    }}
                  >
                    {s.icon}
                  </div>
                  <div>
                    <div style={{ fontSize: 22, fontWeight: 700, color: colors.textPrimary }}>{s.value}</div>
                    <div style={{ fontSize: 12, color: colors.textMuted }}>{s.title}</div>
                  </div>
                </div>
              </Card>
            </Col>
          ))}
        </Row>

        <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
          {subPages.map((p) => (
            <Col xs={24} sm={12} md={6} key={p.title}>
              <div
                onClick={() => navigate(p.path)}
                style={{
                  background: colors.white,
                  border: `1px solid ${colors.border}`,
                  borderRadius: radius.card,
                  padding: '18px 20px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 14,
                  transition: 'all 0.2s',
                }}
              >
                <div
                  style={{
                    width: 40,
                    height: 40,
                    borderRadius: 10,
                    background: `${p.color}15`,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 18,
                    color: p.color,
                  }}
                >
                  {p.icon}
                </div>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 500, color: colors.textPrimary }}>{p.title}</div>
                  <div style={{ fontSize: 12, color: colors.textMuted }}>{p.desc}</div>
                </div>
              </div>
            </Col>
          ))}
        </Row>

        <div className="yx-card">
          <h3
            style={{
              margin: '0 0 16px',
              fontSize: 16,
              fontWeight: 600,
              color: colors.textPrimary,
              paddingBottom: 12,
              borderBottom: `1px solid ${colors.border}`,
            }}
          >
            <FileTextOutlined style={{ marginRight: 8, color: colors.accent }} />
            {t('knowledgeCompile.recentTasks')}
          </h3>
          <Table columns={columns} dataSource={MOCK_COMPILE_TASKS} rowKey="id" pagination={false} size="middle" />
        </div>
      </PageContent>
    </>
  );
}
