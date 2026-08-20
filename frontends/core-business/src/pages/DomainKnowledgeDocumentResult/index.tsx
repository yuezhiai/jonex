import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import { useKbPermission } from '@/hooks/useKbPermission';
import { Button, Card, Space, Tag, Tabs, Spin, Typography, message } from 'antd';
import {
  ArrowLeftOutlined,
  FileTextOutlined,
  ReloadOutlined,
  BuildOutlined,
  FilePdfOutlined,
  BranchesOutlined,
  FileOutlined,
} from '@ant-design/icons';
import {
  getManualDocumentDetail,
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  reparseDocument,
  retryDocumentOntology,
  getDocumentViewTicket,
  getDomainKnowledgeDetail,
} from '@/api/domainKnowledge';
import type { ManualDocItem, OntologyInstanceSummary, RelationInstanceSummary, KnowledgeBaseType } from '@/types/domainKnowledge';
import StageDetailCard from './StageDetailCard';
import CompileResultPanel from './CompileResultPanel';
import type { ProcessingStage } from './StageDetailCard';
import VideoPlayerModal from './VideoPlayerModal';

const { Title } = Typography;

function getDocStatusText(t: (key: string) => string): Record<string, string> {
  return {
    compiled: t('domainKnowledge.docStatus.compiled'),
    parsing: t('domainKnowledge.docStatus.parsing'),
    pending: t('domainKnowledge.docStatus.pending'),
  };
}

/** [jonex] openkb 文档头部状态：解析完成（ready）后按 llm_wiki_compile_status
 *  细化——ready 只代表解析完成，LLM-Wiki 编译是独立异步状态，
 *  不能用 lightrag 的「入库·解析·编译」文案误导用户。 */
function openkbDocStatusText(doc: ManualDocItem, t: (key: string) => string): string {
  if (doc.status !== 'ready') return getDocStatusText(t)[doc.status] || doc.status;
  switch (doc.llmWikiCompileStatus) {
    case 'compiled':
      return t('domainKnowledge.docStatus.openkbCompiled');
    case 'compiling':
      return t('domainKnowledge.docStatus.openkbCompiling');
    case 'failed':
      return t('domainKnowledge.docStatus.openkbCompileFailed');
    case 'stale':
      return t('domainKnowledge.docStatus.openkbCompileStale');
    default:
      return t('domainKnowledge.docStatus.openkbNotCompiled');
  }
}

const DOC_STATUS_COLOR: Record<string, string> = {
  compiled: '#22c55e',
  parsing: '#3b82f6',
  pending: '#94a3b8',
};

export default function DomainKnowledgeDocumentResult() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id = '', docId = '' } = useParams<{ id: string; docId: string }>();
  // KB 写权限（权限矩阵）：重新解析/重新编译按钮依据
  const { canWrite } = useKbPermission(id || undefined);
  const [doc, setDoc] = useState<ManualDocItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState('parse');
  const [activeSubNav, setActiveSubNav] = useState('ontology');
  const [reparseLoading, setReparseLoading] = useState(false);
  const [recompileLoading, setRecompileLoading] = useState(false);
  const [entityTypes, setEntityTypes] = useState<OntologyInstanceSummary[] | null>(null);
  const [relationTypes, setRelationTypes] = useState<RelationInstanceSummary[] | null>(null);
  const [kbType, setKbType] = useState<KnowledgeBaseType>('lightrag');
  const [videoUrl, setVideoUrl] = useState<string>('');
  const [videoOpen, setVideoOpen] = useState(false);
  const [videoTimeStart, setVideoTimeStart] = useState<number | null>(null);

  useEffect(() => {
    if (!id || !docId) return;
    setLoading(true);
    getManualDocumentDetail(id, docId, t)
      .then(setDoc)
      .catch(() => message.error(t('domainKnowledge.documentDetailLoadFailed')))
      .finally(() => setLoading(false));
  }, [id, docId, t]);

  // [jonex] kbType 双来源：doc 详情 metadata.kb_type 优先（与 doc 同一次请求到达，
  // 无竞态——此前独立请求慢/失败时 kbType 停在 lightrag，「编译结果」tab 显示
  // 空本体、刷新后才切 Wiki）；独立 KB 详情请求仅作兜底（旧文档无 kb_type 键）。
  useEffect(() => {
    if (doc?.kbType) setKbType(doc.kbType);
  }, [doc?.kbType]);

  useEffect(() => {
    if (!id || doc?.kbType) return;  // doc 已带 kb_type（同一次请求到达）：无需兜底
    getDomainKnowledgeDetail(id)
      .then((detail) => setKbType(detail.kbType || 'lightrag'))
      .catch(() => {
        // 兜底请求失败：保持默认 lightrag；doc.kbType 存在时上面 effect 已纠正
      });
  }, [id, doc?.kbType]);

  // [jonex] 编译状态轮询：任务化后终态由对账巡检回写（最长 30s 延迟），
  // 前端每 10s 刷新文档详情直到 compiled/failed。仅 openkb KB 轮询
  // （lightrag 文档 llmWikiCompileStatus 恒 undefined，不加门会永不停）。
  // 必须传 t——缺省时 mapBackendDoc 的状态展示串会变成 i18n key 原文。
  useEffect(() => {
    if (!id || !docId || kbType !== 'openkb') return;
    const status = doc?.llmWikiCompileStatus;
    if (status === 'compiled' || status === 'failed') return;   // 终态停止轮询
    const timer = setInterval(() => {
      getManualDocumentDetail(id, docId, t)
        .then((d) => setDoc((prev) => ({ ...prev, ...d })))
        .catch(() => {});
    }, 10_000);
    return () => clearInterval(timer);
  }, [id, docId, kbType, t, doc?.llmWikiCompileStatus]);

  // 本体类型列表只服务于 lightrag 的 OntologyTab/RelationTab 类型筛选；
  // openkb 管线不跑本体抽取，跳过无效请求。
  useEffect(() => {
    if (!id || kbType === 'openkb') return;
    getOntologyEntityTypes(id)
      .then((res) => setEntityTypes(res.items))
      .catch(() => {});
    getOntologyRelationTypes(id)
      .then((res) => setRelationTypes(res.items))
      .catch(() => {});
  }, [id, kbType]);

  // 视频文档：页面加载时自动获取播放 URL
  useEffect(() => {
    if (!docId || doc?.mediaType !== 'video') return;
    getDocumentViewTicket(docId)
      .then(({ url }) => setVideoUrl(url))
      .catch(() => {});
  }, [docId, doc?.mediaType]);

  const handleReparse = async () => {
    setReparseLoading(true);
    try {
      await reparseDocument(docId);
      message.success(t('domainKnowledge.reparseTriggered'));
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.reparseFailed'));
    } finally {
      setReparseLoading(false);
    }
  };

  const handleRecompile = async () => {
    setRecompileLoading(true);
    try {
      await retryDocumentOntology(id, docId);
      // [jonex] openkb KB 的「重新编译」= 重跑 Wiki 编译，提示文案与本体重抽区分
      message.success(
        kbType === 'openkb'
          ? t('domainKnowledge.wikiRecompileTriggered')
          : t('domainKnowledge.recompileTriggered'),
      );
    } catch (err: any) {
      message.error(
        err?.message
          || (kbType === 'openkb'
            ? t('domainKnowledge.wikiRecompileFailed')
            : t('domainKnowledge.retryOntologyFailed')),
      );
    } finally {
      setRecompileLoading(false);
    }
  };

  const fileIcon = (type?: string) => {
    const t = (type || '').toLowerCase();
    if (t === 'pdf') return <FilePdfOutlined style={{ color: '#ef4444', fontSize: 32 }} />;
    return <FileTextOutlined style={{ color: '#3b82f6', fontSize: 32 }} />;
  };

  const stages: ProcessingStage[] = [
    {
      key: 'parse',
      label: t('domainKnowledge.stage.parseResult'),
      icon: <FileOutlined />,
    },
    {
      key: 'compile',
      label: t('domainKnowledge.stage.compileResult'),
      icon: <BranchesOutlined />,
    },
  ];

  const activeStage = stages.find((s) => s.key === activeTab) || stages[0];

  const tabItems = stages.map((stage) => ({
    key: stage.key,
    label: (
      <Space size={6}>
        {stage.icon}
        <span>{stage.label}</span>
      </Space>
    ),
    children:
      stage.key === 'compile' ? (
        <CompileResultPanel
          kbId={id}
          docId={docId}
          kbType={kbType}
          compileStatus={doc?.llmWikiCompileStatus}
          llmWikiCompileError={doc?.llmWikiCompileError}
          activeSubNav={activeSubNav}
          onSubNavChange={setActiveSubNav}
          entityTypes={entityTypes}
          relationTypes={relationTypes}
        />
      ) : (
        <StageDetailCard
          stage={stage}
          docId={docId}
          mediaType={doc?.mediaType}
          onPlayVideo={(timeStart?: number | null) => {
            if (!videoUrl) {
              message.error(t('domainKnowledge.videoLoadFailed'));
              return;
            }
            setVideoTimeStart(timeStart ?? null);
            setVideoOpen(true);
          }}
        />
      ),
  }));

  return (
    <div style={{ padding: '0 0 24px' }}>
      <Spin spinning={loading}>
        <Card
          style={{
            borderRadius: 12,
            border: '1px solid #eef2f6',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            marginBottom: 16,
          }}
          styles={{ body: { padding: '20px 24px' } }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
              flexWrap: 'wrap',
            }}
          >
            <Space align="center" size={16}>
              <Button
                icon={<ArrowLeftOutlined />}
                onClick={() => navigate(`/domain-knowledge/${id}`)}
                style={{ borderRadius: 8 }}
              >
                {t('common.back')}
              </Button>

              {fileIcon(doc?.type)}

              <Space direction="vertical" size={4}>
                <Space align="center" size={12}>
                  <Title level={4} style={{ margin: 0, color: '#0b2b5c', fontSize: 18 }}>
                    {doc?.name || t('domainKnowledge.documentResultTitle')}
                  </Title>
                  <Tag
                    style={{
                      border: 'none',
                      borderRadius: 6,
                      fontSize: 12,
                      color: '#fff',
                      background: DOC_STATUS_COLOR[doc?.status || ''] || '#94a3b8',
                    }}
                  >
                    {kbType === 'openkb'
                      ? (doc ? openkbDocStatusText(doc, t) : t('common.unknown'))
                      : getDocStatusText(t)[doc?.status || ''] || doc?.status || t('common.unknown')}
                  </Tag>
                </Space>

                <Space
                  size={16}
                  style={{
                    color: '#64748b',
                    fontSize: 13,
                    flexWrap: 'wrap',
                  }}
                >
                  <span>{doc?.type?.toUpperCase() || '—'}</span>
                  <span>{doc?.size || '—'}</span>
                  <span>{doc?.uploadTime || '—'}</span>
                </Space>
              </Space>
            </Space>

            <Space size={12}>
              <Button
                icon={<ReloadOutlined />}
                style={{ borderRadius: 8 }}
                loading={reparseLoading}
                disabled={!canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                onClick={handleReparse}
              >
                {t('domainKnowledge.reparse')}
              </Button>
              <Button
                icon={<BuildOutlined />}
                style={{ borderRadius: 8 }}
                loading={recompileLoading}
                disabled={!canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                onClick={handleRecompile}
              >
                {t('domainKnowledge.recompile')}
              </Button>
            </Space>
          </div>
        </Card>

        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={tabItems}
          style={{
            background: '#fff',
            borderRadius: 12,
            border: '1px solid #eef2f6',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            padding: '0 24px 24px',
          }}
        />
      </Spin>

      <VideoPlayerModal
        open={videoOpen}
        videoUrl={videoUrl}
        timeStart={videoTimeStart}
        onClose={() => setVideoOpen(false)}
      />
    </div>
  );
}
