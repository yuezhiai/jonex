import React, { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  RobotOutlined,
  ShareAltOutlined,
  ApartmentOutlined,
  BarsOutlined,
  ReadOutlined,
} from '@ant-design/icons';
import OntologyTab from '@/pages/DomainKnowledgeCompileResults/OntologyTab';
import RelationTab from '@/pages/DomainKnowledgeCompileResults/RelationTab';
import GraphTab from '@/pages/DomainKnowledgeCompileResults/GraphTab';
import ColumnBrowseTab from '@/pages/DomainKnowledgeCompileResults/ColumnBrowseTab';
import WikiPageBrowser from '@/pages/DomainKnowledgeCompileResults/WikiPageBrowser';
import WikiGraphTab from '@/pages/DomainKnowledgeCompileResults/WikiGraphTab';
import WikiReaderShell from '@/pages/DomainKnowledgeCompileResults/wiki/WikiReaderShell';
import WikiColumnBoard from '@/pages/DomainKnowledgeCompileResults/wiki/WikiColumnBoard';
import type { OntologyInstanceSummary, RelationInstanceSummary, KnowledgeBaseType } from '@/types/domainKnowledge';
import { isLlmWikiType } from '@/utils/kbType';

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
  const isLlmWiki = isLlmWikiType(kbType);
  // [jonex] jonex_wiki 与 openkb 是两条链路：前者走新的阅读/图谱/列式三视图
  // （document_id 限定范围），后者保持原来的页面树 + 实体关系图。
  const isJonexWiki = kbType === 'jonex_wiki';

  // [jonex] llm-wiki 分支只有 ontology/graph 两个子导航 key（relation 被移除）。
  // kbType 是异步加载的：页面挂载瞬间还是默认 lightrag（三 Tab），用户若在
  // 此时点了 relation，kbType 返回 llm-wiki 后 navItems 变两个 key，
  // activeSubNav==='relation' 无处命中 → 空白。必须自动回退到 ontology。
  useEffect(() => {
    if (isLlmWiki && (activeSubNav === 'relation' || activeSubNav === 'columnBrowse')) {
      onSubNavChange('ontology');
      return;
    }
    // jonex_wiki 的三个 key 与知识库级完全一致；kbType 异步加载，挂载瞬间可能
    // 还是默认 lightrag 的 key，这里同样兜底回退，避免空白。
    if (isJonexWiki && !['wikiPages', 'wikiColumns'].includes(activeSubNav)) {
      onSubNavChange('wikiPages');
    }
  }, [isLlmWiki, isJonexWiki, activeSubNav, onSubNavChange]);

  const navItems = isJonexWiki
    ? [
        { key: 'wikiPages', label: t('compile.wikiReader'), icon: <ReadOutlined /> },
        { key: 'wikiColumns', label: t('compile.wikiColumns'), icon: <BarsOutlined /> },
      ]
    : isLlmWiki
    ? [
        { key: 'ontology', label: t('compile.wikiPages'), icon: <RobotOutlined /> },
        { key: 'graph', label: t('compile.wikiGraph'), icon: <ApartmentOutlined /> },
      ]
    : [
        { key: 'ontology', label: t('domainKnowledge.ontologyInstances'), icon: <RobotOutlined /> },
        { key: 'relation', label: t('domainKnowledge.relationInstances'), icon: <ShareAltOutlined /> },
        { key: 'graph', label: t('domainKnowledge.graphBreadcrumb'), icon: <ApartmentOutlined /> },
        { key: 'columnBrowse', label: t('domainKnowledge.columnBrowse'), icon: <BarsOutlined /> },
      ];

  // 首层卡面由 PageContent 承担：这里不再套 Card，否则页面是「卡中卡」。
  // 原来靠 Card 的 overflow:hidden 裁掉圆角外的内容，现在由这层盒子自己裁；
  // 高度也不再写死 calc(100vh - Npx) 去猜剩余空间，改由 pane 的 flex 撑满。
  return (
    <div style={{ display: 'flex', flex: 1, minHeight: 0, overflow: 'hidden' }}>
      {/* 左侧子导航：不铺底色，避免与卡片白底叠出第二层"面板"观感，靠 borderRight 分隔 */}
      <div
        style={{
          width: 180,
          borderRight: '1px solid var(--yx-bg-muted)',
          padding: '12px 8px',
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
                color: active ? 'var(--yx-accent)' : 'var(--yx-text-secondary)',
                background: active ? 'var(--yx-info-bg)' : 'transparent',
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
        {/* [jonex] jonex_wiki：与知识库级同一套三视图，document_id 限定范围 */}
        {/* 面板容器是 overflow:hidden 的定高盒子，三视图各自内部滚动 */}
        {isJonexWiki && activeSubNav === 'wikiPages' && (
          // WikiReaderShell 自带「左栏 + 正文」两个内部滚动容器，这里只给确定高度，
          // 不再套外层滚动条（否则会出现双滚动条，且滚到顶的语义会分裂）
          <div style={{ height: '100%', minHeight: 0 }}>
            <WikiReaderShell kbId={kbId} documentId={docId} />
          </div>
        )}
        {isJonexWiki && activeSubNav === 'wikiColumns' && (
          <WikiColumnBoard kbId={kbId} documentId={docId} height="100%" />
        )}
        {!isJonexWiki && activeSubNav === 'ontology' &&
          (isLlmWiki ? (
            <WikiPageBrowser
              kbId={kbId}
              docId={docId}
              compileStatus={compileStatus}
              llmWikiCompileError={llmWikiCompileError}
            />
          ) : (
            <OntologyTab kbId={kbId} docId={docId} data={entityTypes} title={t('domainKnowledge.ontologyInstances')} />
          ))}
        {!isJonexWiki && activeSubNav === 'relation' &&
          !isLlmWiki && (
            <RelationTab
              kbId={kbId}
              docId={docId}
              data={relationTypes}
              title={t('domainKnowledge.relationInstances')}
            />
          )}
        {!isJonexWiki && activeSubNav === 'graph' &&
          // bare：卡面已由 PageContent 承担，图谱不再自带一层壳，否则是「卡中卡」
          (isLlmWiki ? <WikiGraphTab kbId={kbId} docId={docId} /> : <GraphTab kbId={kbId} docId={docId} bare />)}
        {!isJonexWiki && activeSubNav === 'columnBrowse' && !isLlmWiki && (
          <ColumnBrowseTab kbId={kbId} docId={docId} height="100%" />
        )}
      </div>
    </div>
  );
}
