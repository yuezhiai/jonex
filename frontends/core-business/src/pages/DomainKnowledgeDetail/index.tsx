import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Button, Table, Tag, Space, Select, Modal, message } from 'antd';
import './index.scss';
import DataSourceEditModal from './DataSourceEditModal';
import {
  PlusOutlined,
  ArrowLeftOutlined,
  DatabaseOutlined,
  ApiOutlined,
  ControlOutlined,
  CodeOutlined,
  BarChartOutlined,
  ThunderboltOutlined,
  GlobalOutlined,
  FileTextOutlined,
  ShareAltOutlined,
  CloudOutlined,
  UploadOutlined,
  FolderOpenOutlined,
  VideoCameraOutlined,
  BellOutlined,
  PictureOutlined,
  SoundOutlined,
  TeamOutlined,
  BugOutlined,
  EyeOutlined,
  SwapOutlined,
} from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { useStore } from '@/store';
import type {
  DomainKnowledgeDetail as DomainKnowledgeDetailType,
  DataSourceConfig,
  ActionRule,
  RuleTextSegment,
  OntologyInstanceSummary,
  RelationInstanceSummary,
  OntologyStatistics,
  DomainKnowledgePermissionMember,
} from '@/types/domainKnowledge';
import { getStatusTextMap } from '@/types/domainKnowledge';
import {
  getDomainKnowledgeDetail,
  getDomainKnowledgeDataSources,
  getDomainKnowledgeActionRules,
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  getOntologyStatistics,
  getDomainKnowledgePermissions,
  saveDomainKnowledgePermissions,
  type ParserConfigItem,
} from '@/api/domainKnowledge';
import CompileTab from './compile/CompileTab';
import LlmWikiSchemaTab from './llmWikiSchema/LlmWikiSchemaTab';
import SynonymTab from './synonym/SynonymTab';
import DataSourceTab from './DataSourceTab';
import ResultTab from './ResultTab';
import ActionTab from './ActionTab';
import PermissionTab from './PermissionTab';
import AddDataSourceModal from '@/components/datasource/AddDataSourceModal';
import { deleteDataSource } from '@/api/dataSource';
import { ParserConfigContent } from '@/pages/DomainKnowledgeParser';

function getTabs(t: (key: string) => string) {
  return [
    {
      key: 'datasource',
      label: t('domainKnowledge.tab.datasource'),
      icon: ApiOutlined,
    },
    {
      key: 'parse',
      label: t('domainKnowledge.tab.parse'),
      icon: ControlOutlined,
    },
    {
      key: 'compile',
      label: t('domainKnowledge.tab.compile'),
      icon: CodeOutlined,
    },
    // { key: 'result', label: t('domainKnowledge.tab.result'), icon: BarChartOutlined },
    {
      key: 'synonym',
      label: t('domainKnowledge.tab.synonym'),
      icon: SwapOutlined,
    },
    {
      key: 'permission',
      label: t('domainKnowledge.tab.permission'),
      icon: TeamOutlined,
    },
    // { key: 'action', label: 'Action', icon: ThunderboltOutlined },
  ];
}

export const dataSourceIconMap: Record<string, React.ComponentType<any>> = {
  api: CloudOutlined,
  upload: UploadOutlined,
  storage: FolderOpenOutlined,
};

export const actionIconMap: Record<string, React.ComponentType<any>> = {
  video: VideoCameraOutlined,
  bell: BellOutlined,
  picture: PictureOutlined,
  file: FileTextOutlined,
  sound: SoundOutlined,
  bug: BugOutlined,
  team: TeamOutlined,
};

function renderRuleText(segments: RuleTextSegment[]): React.ReactNode {
  return segments.map((seg, i) =>
    seg.bold ? (
      <strong key={i} style={{ color: seg.color || '#0b2b5c' }}>
        {seg.text}
      </strong>
    ) : (
      <React.Fragment key={i}>{seg.text}</React.Fragment>
    ),
  );
}

const DomainKnowledgeDetail = function DomainKnowledgeDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { global } = useStore();
  const currentSpaceId = global.currentSpaceId;
  const [activeTab, setActiveTab] = useState('datasource');
  const dsStatusLabel = (status: string) => {
    const map: Record<string, string> = {
      运行中: 'domainKnowledge.dataSourceStatus.running',
      同步失败: 'domainKnowledge.dataSourceStatus.failed',
      已暂停: 'domainKnowledge.dataSourceStatus.paused',
    };
    return t(map[status] || status);
  };
  const ruleStatusLabel = (status: string) => {
    const map: Record<string, string> = {
      启用: 'status.active',
      停用: 'status.inactive',
    };
    return t(map[status] || status);
  };
  const permissionMemberName = (member: DomainKnowledgePermissionMember) => member.name;

  // ── detail header ────────────────────────────────────
  const [detail, setDetail] = useState<DomainKnowledgeDetailType | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);

  // ── datasource tab ────────────────────────────────────
  const [dataSources, setDataSources] = useState<DataSourceConfig[]>([]);
  const [dataSourcesLoading, setDataSourcesLoading] = useState(false);
  const [addDsOpen, setAddDsOpen] = useState(false);

  // 编辑数据源
  const [editingDs, setEditingDs] = useState<DataSourceConfig | null>(null);

  const openEditModal = (ds: DataSourceConfig) => {
    setEditingDs(ds);
  };

  const reloadDataSources = useCallback(() => {
    if (!id) return;
    setDataSourcesLoading(true);
    getDomainKnowledgeDataSources(id, t)
      .then(setDataSources)
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDataSourcesFailed')))
      .finally(() => setDataSourcesLoading(false));
  }, [id, t]);

  // ── result tab ────────────────────────────────────────
  const [ontologySummaries, setOntologySummaries] = useState<OntologyInstanceSummary[]>([]);
  const [relationSummaries, setRelationSummaries] = useState<RelationInstanceSummary[]>([]);
  const [resultStats, setResultStats] = useState<OntologyStatistics | null>(null);
  const [resultLoading, setResultLoading] = useState(false);

  // ── action tab ────────────────────────────────────────
  const [actionRules, setActionRules] = useState<ActionRule[]>([]);
  const [actionLoading, setActionLoading] = useState(false);

  // ── permission tab ────────────────────────────────────
  const [permissionMembers, setPermissionMembers] = useState<DomainKnowledgePermissionMember[]>([]);
  const [permissionLoading, setPermissionLoading] = useState(false);
  const [permissionSaving, setPermissionSaving] = useState(false);
  const [permissionKeyword, setPermissionKeyword] = useState('');
  const permDebounceRef = useRef<ReturnType<typeof setTimeout>>();

  const reloadPermissions = useCallback(() => {
    if (!id) return;
    setPermissionLoading(true);
    getDomainKnowledgePermissions(id, permissionKeyword || undefined)
      .then((data) => setPermissionMembers(data.members))
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchPermissionMembersFailed')))
      .finally(() => setPermissionLoading(false));
  }, [id, permissionKeyword, t]);

  const handlePermKeywordChange = (val: string) => {
    setPermissionKeyword(val);
    if (permDebounceRef.current) clearTimeout(permDebounceRef.current);
    permDebounceRef.current = setTimeout(() => {
      if (!id) return;
      setPermissionLoading(true);
      getDomainKnowledgePermissions(id, val || undefined)
        .then((data) => setPermissionMembers(data.members))
        .catch(() => {
          /* silent */
        })
        .finally(() => setPermissionLoading(false));
    }, 300);
  };

  const handlePermAddMember = (user: import('@/api/user').PlatformUser) => {
    setPermissionMembers((prev) => {
      if (prev.some((m) => m.userId === String(user.id))) return prev;
      return [
        ...prev,
        {
          userId: String(user.id),
          name: user.display_name || user.username,
          dept: '',
          avatarText: (user.display_name || user.username).charAt(0).toUpperCase(),
          avatarColor: '#94a3b8',
          role: 'viewer',
        },
      ];
    });
  };

  const handlePermRoleChange = (userId: string, role: 'viewer' | 'editor') => {
    setPermissionMembers((prev) => prev.map((m) => (m.userId === userId ? { ...m, role } : m)));
  };

  const handleSavePermissions = useCallback(async () => {
    if (!id) return;
    setPermissionSaving(true);
    try {
      await saveDomainKnowledgePermissions(id, {
        members: permissionMembers.map((m) => ({
          userId: m.userId,
          role: m.role,
        })),
      });
      message.success(t('domainKnowledge.savePermissionSuccess'));
    } catch (err: any) {
      message.error(err?.message || t('domainKnowledge.savePermissionFailed'));
    } finally {
      setPermissionSaving(false);
    }
  }, [id, permissionMembers, t]);

  const loadedTabsRef = useRef<Set<string>>(new Set());

  // ── fetch detail ──────────────────────────────────────
  useEffect(() => {
    if (!id) return;
    setDetailLoading(true);
    getDomainKnowledgeDetail(id)
      .then(setDetail)
      .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDetailFailed')))
      .finally(() => setDetailLoading(false));
  }, [id, t]);

  // ── load tab data lazily ──────────────────────────────
  const loadTabData = useCallback(
    (tabKey: string) => {
      if (!id || loadedTabsRef.current.has(tabKey)) return;
      loadedTabsRef.current.add(tabKey);

      switch (tabKey) {
        case 'datasource':
          setDataSourcesLoading(true);
          getDomainKnowledgeDataSources(id, t)
            .then(setDataSources)
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchDataSourcesFailed')))
            .finally(() => setDataSourcesLoading(false));
          break;

        case 'result':
          setResultLoading(true);
          Promise.all([getOntologyStatistics(id), getOntologyEntityTypes(id), getOntologyRelationTypes(id)])
            .then(([stats, entityTypes, relationTypes]) => {
              setResultStats(stats);
              setOntologySummaries(entityTypes.items);
              setRelationSummaries(relationTypes.items);
            })
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchResultDataFailed')))
            .finally(() => setResultLoading(false));
          break;

        case 'action':
          setActionLoading(true);
          getDomainKnowledgeActionRules(id)
            .then(setActionRules)
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchActionRulesFailed')))
            .finally(() => setActionLoading(false));
          break;

        case 'permission':
          setPermissionLoading(true);
          getDomainKnowledgePermissions(id)
            .then((data) => setPermissionMembers(data.members))
            .catch((err: any) => message.error(err?.message || t('domainKnowledge.fetchPermissionMembersFailed')))
            .finally(() => setPermissionLoading(false));
          break;
      }
    },
    [id, t],
  );

  useEffect(() => {
    loadTabData(activeTab);
  }, [activeTab, loadTabData]);

  // ── render helpers ────────────────────────────────────
  const renderSectionHeader = (title: string, icon: React.ReactNode, showAdd = true) => (
    <div className="yx-kb-flex-header">
      <h3 className="yx-kb-section-title">
        {icon} {title}
      </h3>
      {showAdd && (
        <Button type="primary" className="yx-kb-section-add-btn" icon={<PlusOutlined />}>
          {t('domainKnowledge.newWithTitle', {
            title: title.replace('设置', '').replace('管理', ''),
          })}
        </Button>
      )}
    </div>
  );

  // ── render tab content ────────────────────────────────
  const renderTabContent = () => {
    switch (activeTab) {
      case 'datasource':
        return (
          <DataSourceTab
            dataSources={dataSources}
            dataSourcesLoading={dataSourcesLoading}
            canWrite={detail?.can_write ?? false}
            onAdd={() => setAddDsOpen(true)}
            onEdit={(ds) => openEditModal(ds)}
            onReload={reloadDataSources}
          />
        );

      case 'parse':
        return <ParserConfigContent kbId={id} spaceId={currentSpaceId} canWrite={detail?.can_write ?? false} />;

      case 'compile':
        // [jonex] 管线分流（方案 §10）：openkb KB 显示 LLM-Wiki Schema 设置；
        // detail?.kbType 已随 KB 详情加载（无额外请求）；未到达前按 lightrag 渲染
        return detail?.kbType === 'openkb'
          ? <LlmWikiSchemaTab kbId={id!} canWrite={detail?.can_write ?? false} />
          : <CompileTab kbId={id!} canWrite={detail?.can_write ?? false} />;

      case 'synonym':
        return <SynonymTab kbId={id!} canWrite={detail?.can_write ?? false} />;

      case 'result':
        return (
          <ResultTab
            resultStats={resultStats}
            ontologySummaries={ontologySummaries}
            relationSummaries={relationSummaries}
            resultLoading={resultLoading}
          />
        );

      case 'action':
        return (
          <ActionTab
            actionRules={actionRules}
            actionLoading={actionLoading}
            ruleStatusLabel={ruleStatusLabel}
            renderRuleText={renderRuleText}
          />
        );

      case 'permission':
        return (
          <PermissionTab
            members={permissionMembers}
            loading={permissionLoading}
            saving={permissionSaving}
            canManage={detail?.can_manage_permissions ?? false}
            memberName={permissionMemberName}
            onAdd={handlePermAddMember}
            onRoleChange={handlePermRoleChange}
            onRemove={(userId) => setPermissionMembers((prev) => prev.filter((m) => m.userId !== userId))}
            onSave={handleSavePermissions}
          />
        );

      default:
        return null;
    }
  };

  // ── main render ──
  return (
    <div
      style={{
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        color: '#1e293b',
        letterSpacing: 0,
      }}
    >
      {/* Back Navigation */}
      <div style={{ marginBottom: 16 }}>
        <a onClick={() => navigate('/domain-knowledge')} className="yx-kb-back-link">
          <ArrowLeftOutlined style={{ fontSize: 12 }} /> {t('domainKnowledge.backToKnowledgeList')}
        </a>
      </div>

      {/* Knowledge Base Header */}
      <div className="yx-kb-header-card">
        <div className="yx-kb-header-icon">
          <DatabaseOutlined />
        </div>
        {detailLoading ? (
          <div style={{ flex: 1 }}>
            <h2 className="yx-kb-name-lg">{t('common.loading')}</h2>
          </div>
        ) : detail ? (
          <>
            <div style={{ flex: 1 }}>
              <h2 className="yx-kb-name-lg">{detail.name}</h2>
              <div className="yx-kb-tag-row">
                <span className="yx-kb-stat-badge">
                  <GlobalOutlined style={{ color: '#3b82f6' }} /> {detail.spaceName}
                </span>
                <span className="yx-kb-stat-badge">
                  <FileTextOutlined /> {detail.documentCount.toLocaleString()} {t('domainKnowledge.documents')}
                </span>
                <span className="yx-kb-stat-badge">
                  <ShareAltOutlined /> {detail.entityCount.toLocaleString()} {t('domainKnowledge.entities')}
                </span>
                <span className="yx-kb-stat-badge">
                  <ShareAltOutlined /> {detail.relationCount.toLocaleString()} {t('domainKnowledge.relations')}
                </span>
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className={`yx-kb-detail-status ${detail.status}`}>{getStatusTextMap(t)[detail.status]}</div>
              <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 2 }}>
                {t('domainKnowledge.lastUpdated', { time: detail.updatedAt })}
              </div>
            </div>
          </>
        ) : null}
      </div>

      {/* Tab Bar */}
      <div className="yx-kb-detail-tabs">
        {getTabs(t).map((tab) => {
          const Icon = tab.icon;
          const active = activeTab === tab.key;
          return (
            <div
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`yx-kb-detail-tab${active ? ' active' : ''}`}
            >
              <Icon /> {tab.label}
            </div>
          );
        })}
      </div>

      {renderTabContent()}

      {/* 编辑数据源弹窗 */}
      <DataSourceEditModal
        editingDs={editingDs}
        onClose={() => setEditingDs(null)}
        onSaved={() => {
          setEditingDs(null);
          reloadDataSources();
        }}
      />

      <AddDataSourceModal
        open={addDsOpen}
        kbId={id!}
        existingTypes={dataSources.map((ds) => ds.accessType)}
        onClose={() => setAddDsOpen(false)}
        onCreated={() => {
          setAddDsOpen(false);
          reloadDataSources();
        }}
      />
    </div>
  );
};

export default DomainKnowledgeDetail;
