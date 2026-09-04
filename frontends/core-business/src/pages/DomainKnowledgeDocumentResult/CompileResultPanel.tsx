import React, { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Card } from 'antd';
import { RobotOutlined, ShareAltOutlined, ApartmentOutlined } from '@ant-design/icons';
import OntologyTab from '@/pages/DomainKnowledgeCompileResults/OntologyTab';
import RelationTab from '@/pages/DomainKnowledgeCompileResults/RelationTab';
import GraphTab from '@/pages/DomainKnowledgeCompileResults/GraphTab';
import WikiPageBrowser from '@/pages/DomainKnowledgeCompileResults/WikiPageBrowser';
import WikiGraphTab from '@/pages/DomainKnowledgeCompileResults/WikiGraphTab';
import type { OntologyInstanceSummary, RelationInstanceSummary, KnowledgeBaseType } from '@/types/domainKnowledge';

interface CompileResultPanelProps {
  kbId: string;
  /** [jonex] 收紧为必传：index.tsx useParams 解构默认 ''，运行时恒为 string。 */
  docId: string;
  kbType?: KnowledgeBaseType;
  /** [jonex] LLM-Wiki 编译状态（llm_wiki_compile_status），未编译文档的引导用。 */
  compileStatus?: string;
  llmWikiCompileError?: string;
  activeSubNav: string;
  onSubNavChange: (key: string) => void;
  entityTypes: OntologyInstanceSummary[] | null;
  relationTypes: RelationInstanceSummary[] | null;
}

export default function CompileResultPanel({
  kbId,
  docId,
  kbType = 'lightrag',
  compileStatus,
  llmWikiCompileError,
  activeSubNav,
  onSubNavChange,
  entityTypes,
  relationTypes,
}: CompileResultPanelProps) {
  const { t } = useTranslation();
  const isOpenKB = kbType === 'openkb';

  // [jonex] openkb 分支只有 ontology/graph 两个子导航 key（relation 被移除）。
  // kbType 是异步加载的：页面挂载瞬间还是默认 lightrag（三 Tab），用户若在
  // 此时点了 relation，kbType 返回 openkb 后 navItems 变两个 key，
  // activeSubNav==='relation' 无处命中 → 空白。必须自动回退到 ontology。
  useEffect(() => {
    if (isOpenKB && activeSubNav === 'relation') {
      onSubNavChange('ontology');
    }
  }, [isOpenKB, activeSubNav, onSubNavChange]);

  const navItems = isOpenKB
    ? [
        { key: 'ontology', label: t('compile.wikiPages'), icon: <RobotOutlined /> },
        { key: 'graph', label: t('compile.wikiGraph'), icon: <ApartmentOutlined /> },
      ]
    : [
        { key: 'ontology', label: t('domainKnowledge.ontologyInstances'), icon: <RobotOutlined /> },
        { key: 'relation', label: t('domainKnowledge.relationInstances'), icon: <ShareAltOutlined /> },
        { key: 'graph', label: t('domainKnowledge.graphBreadcrumb'), icon: <ApartmentOutlined /> },
      ];

  return (
    <Card
      style={{
        borderRadius: 12,
        border: '1px solid #eef2f6',
        boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
        overflow: 'hidden',
      }}
      styles={{ body: { padding: 0 } }}
    >
      <div style={{ display: 'flex', height: 'calc(100vh - 300px)', minHeight: 500, overflow: 'hidden' }}>
        <div
          style={{
            width: 180,
            borderRight: '1px solid #f1f5f9',
            padding: '12px 8px',
            background: '#fafbfc',
            flexShrink: 0,
            overflowY: 'auto',
          }}
        >
          {navItems.map((item) => {
            const active = activeSubNav === item.key;
            return (
              <div
                key={item.key}
                onClick={() => onSubNavChange(item.key)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '12px 16px',
                  borderRadius: 8,
                  cursor: 'pointer',
                  fontSize: 14,
                  fontWeight: 500,
                  color: active ? '#3b82f6' : '#64748b',
                  background: active ? '#eff6ff' : 'transparent',
                  transition: 'all 0.2s',
                  marginBottom: 4,
                }}
              >
                {item.icon}
                <span>{item.label}</span>
              </div>
            );
          })}
        </div>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          {activeSubNav === 'ontology' &&
            (isOpenKB ? (
              <WikiPageBrowser
                kbId={kbId}
                docId={docId}
                compileStatus={compileStatus}
                llmWikiCompileError={llmWikiCompileError}
              />
            ) : (
              <OntologyTab kbId={kbId} docId={docId} data={entityTypes} title={t('domainKnowledge.ontologyInstances')} />
            ))}
          {activeSubNav === 'relation' &&
            !isOpenKB && (
              <RelationTab
                kbId={kbId}
                docId={docId}
                data={relationTypes}
                title={t('domainKnowledge.relationInstances')}
              />
            )}
          {activeSubNav === 'graph' &&
            (isOpenKB ? <WikiGraphTab kbId={kbId} docId={docId} /> : <GraphTab kbId={kbId} docId={docId} />)}
        </div>
      </div>
    </Card>
  );
}
