import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';
import { useKbPermission } from '@/hooks/useKbPermission';
import { Button, Space, Tag, Tabs, Spin, message } from 'antd';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import {
  FileTextOutlined,
  ReloadOutlined,
  BuildOutlined,
  FilePdfOutlined,
  BranchesOutlined,
  FileOutlined,
  FileSearchOutlined,
  HddOutlined,
  ClockCircleOutlined,
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
import DocumentQualityCard from '@/components/DocumentQualityCard';
import CompileResultPanel from './CompileResultPanel';
import DocumentVersionsTab from './DocumentVersionsTab';
import { buildDocumentStageKeys } from './documentTabs';
import type { DocumentStageKey } from './documentTabs';
import type { ProcessingStage } from './StageDetailCard';
import VideoPlayerModal from './VideoPlayerModal';
import { isLlmWikiType } from '@/utils/kbType';
import './index.scss';

function getDocStatusText(t: (key: string) => string): Record<string, string> {
  return {
    compiled: t('domainKnowledge.docStatus.compiled'),
    parsing: t('domainKnowledge.docStatus.parsing'),
    pending: t('domainKnowledge.docStatus.pending'),
  };
}

/** [jonex] llm-wiki 文档头部状态：解析完成（ready）后按 llm_wiki_compile_status
 *  细化——ready 只代表解析完成，LLM-Wiki 编译是独立异步状态，
 *  不能用 lightrag 的「入库·解析·编译」文案误导用户。 */
function llmWikiDocStatusText(doc: ManualDocItem, t: (key: string) => string): string {
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
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.documentDetailLoadFailed')))
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
  // 前端每 10s 刷新文档详情直到 compiled/failed。仅 llm-wiki KB 轮询
  // （lightrag 文档 llmWikiCompileStatus 恒 undefined，不加门会永不停）。
  // 必须传 t——缺省时 mapBackendDoc 的状态展示串会变成 i18n key 原文。
  useEffect(() => {
    if (!id || !docId || !isLlmWikiType(kbType)) return;
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
  // llm-wiki 管线不跑本体抽取，跳过无效请求。
  useEffect(() => {
    if (!id || isLlmWikiType(kbType)) return;
    getOntologyEntityTypes(id, undefined, docId)
      .then((res) => setEntityTypes(res.items))
      .catch(() => {});
    getOntologyRelationTypes(id, docId)
      .then((res) => setRelationTypes(res.items))
      .catch(() => {});
  }, [id, docId, kbType]);

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
      // [jonex] llm-wiki KB 的「重新编译」= 重跑 Wiki 编译，提示文案与本体重抽区分
      message.success(
        isLlmWikiType(kbType)
          ? t('domainKnowledge.wikiRecompileTriggered')
          : t('domainKnowledge.recompileTriggered'),
      );
    } catch (err: any) {
      message.error(
        err?.message
          || (isLlmWikiType(kbType)
            ? t('domainKnowledge.wikiRecompileFailed')
            : t('domainKnowledge.retryOntologyFailed')),
      );
    } finally {
      setRecompileLoading(false);
    }
  };

  const fileIcon = (type?: string) => {
    const t = (type || '').toLowerCase();
    // 与知识库详情页头同尺寸：页标题本身是 24px，图标跟着标题走
    if (t === 'pdf') return <FilePdfOutlined style={{ color: '#ef4444', fontSize: 24 }} />;
    return <FileTextOutlined style={{ color: '#3b82f6', fontSize: 24 }} />;
  };

  // 质量报告独立成 Tab：它与片段查看器同处一个纵向 flex 时，报告高度会挤占
  // 片段区；无报告的空态块也会把原文片段往下推（2026-10-18 用户反馈）。
  const stageMeta: Record<DocumentStageKey, { label: string; icon: React.ReactNode }> = {
    parse: {
      label: t('domainKnowledge.stage.parseResult'),
      icon: <FileOutlined />,
    },
    quality: {
      label: t('domainKnowledge.stage.qualityReport'),
      icon: <FileSearchOutlined />,
    },
    compile: {
      label: t('domainKnowledge.stage.compileResult'),
      icon: <BranchesOutlined />,
    },
  };
  const stages: ProcessingStage[] = buildDocumentStageKeys().map(
    (key) => ({ key, ...stageMeta[key] }),
  );

  const tabItems: Array<{
    key: string;
    label: React.ReactNode;
    children: React.ReactNode;
  }> = stages.map((stage) => ({
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
      ) : stage.key === 'quality' ? (
        <div className="yx-doc-result-scroll">
          <DocumentQualityCard
            kbId={id}
            docId={docId}
            refreshKey={doc?.updatedAt}
            canWrite={canWrite}
            onReparse={handleReparse}
          />
        </div>
      ) : (
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <StageDetailCard
            stage={stage}
            docId={docId}
            refreshKey={kbType === 'jonex_wiki' ? doc?.updatedAt : undefined}
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
        </div>
      ),
  }));

  // 版本链对所有文档类型都适用：新版生效、旧版仅存档可下载。
  tabItems.push({
    key: 'versions',
    label: (
      <Space size={6}>
        <ClockCircleOutlined />
        <span>{t('domainKnowledge.stage.versions')}</span>
      </Space>
    ),
    children: (
      <div className="yx-doc-result-scroll">
        <DocumentVersionsTab
          kbId={id}
          docId={docId}
          canWrite={canWrite}
          onVersionApplied={() => {
            if (id && docId) {
              getManualDocumentDetail(id, docId, t)
                .then(setDoc)
                .catch(() => undefined);
            }
          }}
        />
      </div>
    ),
  });

  return (
    <>
      {/* 页头承担返回与文档身份：返回箭头、文件图标 + 名称 + 状态标签排一行，
          类型/大小/上传时间三个元信息与两个操作按钮同行排在右侧 */}
      <PageHeader
        backTo={`/domain-knowledge/${id}`}
        title={
          // wrap：名称 + 状态标签在窄屏放不下时换行，而不是把页头撑出横向滚动
          <Space align="center" size={10} wrap>
            {fileIcon(doc?.type)}
            {/* 名称要等详情到货，加载中先显示文案而不是空白页头 */}
            <span>{loading ? t('common.loading') : doc?.name || t('domainKnowledge.documentResultTitle')}</span>
            <Tag
              style={{
                border: 'none',
                borderRadius: 6,
                fontSize: 12,
                color: '#fff',
                // Tag 自带 margin-inline-end，会在 Space 的 gap 之外再多一份间距，这里清掉
                margin: 0,
                background: DOC_STATUS_COLOR[doc?.status || ''] || '#94a3b8',
              }}
            >
              {isLlmWikiType(kbType)
                ? (doc ? llmWikiDocStatusText(doc, t) : t('common.unknown'))
                : getDocStatusText(t)[doc?.status || ''] || doc?.status || t('common.unknown')}
            </Tag>

            {/* 类型 / 大小 / 上传时间：与状态标签同一行的浅底胶囊，形状取自知识库详情页头的统计徽标。
                与状态标签拉开一点距离，避免四个标签连成一串分不清主次 */}
            <Space size={6} align="center" wrap style={{ marginLeft: 4 }}>
              <Tag icon={<FileOutlined />} style={{ margin: 0 }}>
                {doc?.type?.toUpperCase() || '—'}
              </Tag>
              <Tag icon={<HddOutlined />} style={{ margin: 0 }}>
                {doc?.size || '—'}
              </Tag>
              <Tag icon={<ClockCircleOutlined />} style={{ margin: 0 }}>
                {doc?.uploadTime || '—'}
              </Tag>
            </Space>
          </Space>
        }
        extra={
          // marginLeft:auto 吃掉标题之后的剩余空间：extra 本身是 flex item 不会自己靠右
          //（PageHeader 的标题设了 flex:0 1 auto，只保证标题不被顶开），
          // 不加这一条按钮会紧跟在标题后面，右缘与内容卡片对不齐
          <Space size={12} style={{ marginLeft: 'auto' }}>
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
        }
      />

      <PageContent>
        {/* 卡片外壳由 PageContent 承担，Tabs 不再自带白底与边框。
            这层 div 只为把卡片高度递给 Tabs：中间的 Spin 会渲染 .ant-spin /
            .ant-spin-container 两层包裹，它们都是内容高度，不撑满的话
            Tabs 的 100% 无处可依，内容区会塌成内容高度 */}
        <div className="yx-doc-result-fill">
          <Spin spinning={loading}>
            <Tabs
              className="yx-doc-result-tabs"
              activeKey={activeTab}
              onChange={setActiveTab}
              items={tabItems}
            />
          </Spin>
        </div>
      </PageContent>

      <VideoPlayerModal
        open={videoOpen}
        videoUrl={videoUrl}
        timeStart={videoTimeStart}
        onClose={() => setVideoOpen(false)}
      />
    </>
  );
}
