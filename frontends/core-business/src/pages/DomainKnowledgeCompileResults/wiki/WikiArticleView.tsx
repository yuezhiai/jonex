/**
 * [jonex] Wiki 阅读界面「条目页」：维基百科式的文章视图。
 *
 * 页面结构（与维基百科条目对应）：
 *   标题 + 类型徽标
 *   导语（引擎 `## Summary`）
 *   信息框（别名 / 分类 / 标签 / 来源文档数 / 阅读提示）
 *   页内目录（正文 h2~h4）
 *   正文（蓝字 wikilink 可点击跳转；解析不到的目标降级为纯文本——不做红链）
 *   相关页面（引擎页间链接 ∩ 有效页面）
 *   参考来源 References（页面级溯源 → 打开**原始文档**）
 *   所属分类 / 标签
 */
import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Tag, Tooltip, Empty } from 'antd';
import { BookOutlined, FileTextOutlined, LinkOutlined, NumberOutlined } from '@ant-design/icons';
import MarkdownContent from '@/components/MarkdownContent';
import WikiSourcePicture from '@/components/WikiSourcePicture';
import { useDocumentViewer, inferMediaType } from '@/components/DocumentViewer';
import type { WikiBacklinkResult, WikiPageContent, WikiPageReference, WikiReaderPage } from '@/types/domainKnowledge';
import WikiBacklinks from './WikiBacklinks';
import WikiTableReader from './WikiTableReader';
import WikiLinkGroups from './WikiLinkGroups';
import { buildLinkGroups } from './wikiRelations';
import { analyzePageLinks, isRepeatedWikiLead, roleLabelKey } from './wikiMarkdown';

/** 「相关页面」至少给这么多（不足时用同分类页补齐，保证条目不是死胡同） */
const SEE_ALSO_MIN = 5;

/** 共享标签数（同分类补齐时的排序依据：越相关越靠前）。 */
function sharedTagCount(page: WikiReaderPage, current?: WikiReaderPage): number {
  if (!current || !page.tags?.length || !current.tags?.length) return 0;
  const currentTags = new Set(current.tags);
  return page.tags.filter((tag) => currentTags.has(tag)).length;
}

interface WikiArticleViewProps {
  page: WikiPageContent;
  pages: WikiReaderPage[];
  pagesByPath: Map<string, WikiReaderPage>;
  /** 目录中所在位置（面包屑第二段由 shell 渲染，这里只显示「所在章节」） */
  location?: { topicId: string; topicTitle: string; trailText: string };
  /** 链入本页（反向链接）结果，由 shell 统一取一次，图谱与列表共用 */
  backlinks?: WikiBacklinkResult | null;
  backlinksLoading?: boolean;
  onOpenPage: (path: string) => void;
  onOpenTopic: (nodeId: string) => void;
}

const roleColor = (role: string) =>
  role === 'concept' ? 'blue' : role === 'table' ? 'purple' : role === 'source' ? 'green' : 'default';

export default function WikiArticleView({
  page,
  pages,
  pagesByPath,
  location,
  backlinks,
  backlinksLoading,
  onOpenPage,
  onOpenTopic,
}: WikiArticleViewProps) {
  const { t } = useTranslation();
  const { openDocument, viewer } = useDocumentViewer();

  const canonicalPath = page.enginePath || page.path;
  // 引擎 frontmatter 偶尔没有标题 → 回退目录清单里的标题，避免 h1 空白
  const catalogPage = pagesByPath.get(canonicalPath);
  const displayTitle = page.title || catalogPage?.title || canonicalPath;

  // 正文分段 + 链接解析 + 首次提及自动链接 + 页内目录（纯函数，见 wikiMarkdown.ts）
  const { leadMarkdown, bodyMarkdown, toc, mentionPaths } = useMemo(
    () => analyzePageLinks(page.content || '', pages, canonicalPath),
    [page.content, pages, canonicalPath],
  );

  // 相关页面：显式页间链接 → 正文提及 → 同分类补齐（保证任何条目都有横向出口）
  const seeAlso = useMemo(() => {
    const explicitCandidates = [...(page.links || []), ...(page.related || [])];
    const seen = new Set<string>();
    const out: WikiReaderPage[] = [];
    const push = (path: string) => {
      if (!path || path === canonicalPath || seen.has(path) || out.length >= 12) return;
      const target = pagesByPath.get(path);
      if (!target) return; // 目标不是可见页面（raw/evidence 等）→ 不展示，避免死链
      seen.add(path);
      out.push(target);
    };
    for (const raw of explicitCandidates) push(`${raw}`.replace(/^\//, ''));
    for (const path of mentionPaths) push(path);
    if (out.length < SEE_ALSO_MIN) {
      const category = catalogPage?.category || '';
      const siblings = pages
        .filter((p) => p.path !== canonicalPath && p.category && p.category === category)
        .sort((a, b) => sharedTagCount(b, catalogPage) - sharedTagCount(a, catalogPage));
      for (const sibling of siblings) {
        push(sibling.path);
        if (out.length >= SEE_ALSO_MIN) break;
      }
    }
    return out;
  }, [page.links, page.related, mentionPaths, pagesByPath, pages, canonicalPath, catalogPage]);

  const references = page.references || [];
  const role = page.type || 'concept';

  // 本页关联（右栏列表）：与图谱 Tab 的星形图共用同一个分组模型。
  // 不含「链入本页」——正文里已有一段完整的「链入本页」（带提及次数），不重复。
  const linkGroups = useMemo(
    () =>
      buildLinkGroups({
        page,
        pagesByPath,
        mentionPaths,
        backlinks: backlinks?.items || [],
        seeAlso,
        includeBacklinks: false,
      }),
    [page, pagesByPath, mentionPaths, backlinks, seeAlso],
  );

  const openReference = (documentId: string, fileName: string, mimeType: string) => {
    openDocument({
      docId: documentId,
      fileName: fileName || documentId,
      mediaType: inferMediaType(fileName || mimeType),
    });
  };

  const openPageReference = (ref: WikiPageReference) => openReference(ref.documentId, ref.fileName, ref.mimeType);
  const openImageSource = (image: NonNullable<WikiPageContent['images']>[number]) =>
    openReference(image.document_id, image.source_name, '');

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        {/* 标题 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h1 style={{ margin: 0, fontSize: 26, color: '#0b2b5c' }}>{displayTitle}</h1>
          <Tag color={roleColor(role)} style={{ borderRadius: 6 }}>
            {t(roleLabelKey(role))}
          </Tag>
          {location && (
            <Tooltip title={t('domainKnowledge.wikiReader.locateInOutline')}>
              <a style={{ fontSize: 12, color: '#94a3b8' }} onClick={() => onOpenTopic(location.topicId)}>
                <BookOutlined /> {location.trailText}
              </a>
            </Tooltip>
          )}
        </div>

        {/* 导语 */}
        {leadMarkdown ? (
          <div style={{ marginTop: 10, fontSize: 15, lineHeight: 1.9, color: '#334155' }}>
            <MarkdownContent content={leadMarkdown} onInternalLink={onOpenPage} images={page.images} onOpenImageSource={openImageSource} />
          </div>
        ) : (
          page.description && !isRepeatedWikiLead(page.description, bodyMarkdown) && <div style={{ marginTop: 10, fontSize: 15, color: '#475569' }}>{page.description}</div>
        )}

        {/* 页内目录 */}
        {!page.tableData && toc.length > 1 && (
          <div
            style={{
              display: 'inline-block',
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 8,
              padding: '10px 16px',
              margin: '16px 0 4px',
              minWidth: 220,
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, color: '#0b2b5c', marginBottom: 6 }}>
              <NumberOutlined /> {t('domainKnowledge.wikiReader.inPageToc')}
            </div>
            {toc
              .filter((item) => item.level <= 3)
              .map((item) => (
                <div
                  key={item.id}
                  style={{
                    fontSize: 13,
                    paddingLeft: (item.level - 2) * 14,
                    lineHeight: 1.9,
                  }}
                >
                  <a href={`#${item.id}`} style={{ color: '#2563eb' }}>
                    {item.text}
                  </a>
                </div>
              ))}
          </div>
        )}

        {/* 正文 */}
        <div style={{ marginTop: 12 }}>
          {page.tableData ? <WikiTableReader key={`${page.tableData.revision}:${page.tableData.selected_table_id}`}
            data={page.tableData} fileName={references.find(ref => ref.documentId === page.tableData?.document_id)?.fileName || ''}
            onOpenSource={() => {
              const ref = references.find(item => item.documentId === page.tableData?.document_id);
              if (ref) openPageReference(ref);
              else openReference(page.tableData!.document_id, page.tableData!.document_id, '');
            }} /> : <MarkdownContent content={bodyMarkdown} onInternalLink={onOpenPage} headingIds={toc.map((item) => item.id)} images={page.images} onOpenImageSource={openImageSource} />}
        </div>

        {(page.images || []).filter((image) => image.placement !== 'inline').map((image) =>
          <WikiSourcePicture key={image.asset_id} image={image} onOpenSource={openImageSource} />)}
        {page.imageError && <div role="status" style={{ color: '#64748b' }}>图片暂不可读，正文仍可阅读。请查看原文。</div>}
        {(page.imagesTotal || 0) > (page.images?.length || 0) && <p style={{ color: '#64748b' }}>本页还有更多原文图片，可通过顶部「找图」分页浏览。</p>}

        {/* 相关页面 */}
        {seeAlso.length > 0 && (
          <div style={{ marginTop: 26 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: '#0b2b5c', marginBottom: 8 }}>
              <LinkOutlined /> {t('domainKnowledge.wikiReader.seeAlso')}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {seeAlso.map((target) => (
                <a
                  key={target.path}
                  onClick={() => onOpenPage(target.path)}
                  style={{
                    color: '#2563eb',
                    fontSize: 14,
                    padding: '2px 10px',
                    background: '#f8fafc',
                    borderRadius: 14,
                    border: '1px solid #e2e8f0',
                  }}
                >
                  {target.title}
                </a>
              ))}
            </div>
          </div>
        )}

        {/* 链入本页（What links here）：谁引用了这一页 */}
        <WikiBacklinks result={backlinks || null} loading={backlinksLoading} onOpenPage={onOpenPage} />

        {/* 参考来源：页面级溯源 → 源文档 */}
        <div style={{ marginTop: 26 }}>
          <div style={{ fontSize: 16, fontWeight: 600, color: '#0b2b5c', marginBottom: 8 }}>
            <FileTextOutlined /> {t('domainKnowledge.wikiReader.references')}
            {references.length > 0 && (
              <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 400, marginLeft: 8 }}>
                {t('domainKnowledge.wikiReader.referencesCount', { count: references.length })}
              </span>
            )}
          </div>
          {references.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('domainKnowledge.wikiReader.noReferences')}
              style={{ margin: '8px 0' }}
            />
          ) : (
            <ol style={{ paddingLeft: 20, margin: 0, fontSize: 13, color: '#475569' }}>
              {references.map((ref) => (
                <li key={ref.documentId} style={{ marginBottom: 10, lineHeight: 1.7 }}>
                  <a
                    onClick={() => openReference(ref.documentId, ref.fileName, ref.mimeType)}
                    style={{ color: '#2563eb', cursor: 'pointer' }}
                  >
                    {ref.fileName || ref.documentId.slice(0, 12)}
                  </a>
                  <span style={{ color: '#cbd5e1', marginLeft: 8 }}>
                    {t('domainKnowledge.wikiReader.claimCount', { count: ref.claimCount })}
                  </span>
                  {ref.excerpts.slice(0, 1).map((excerpt) => (
                    <div
                      key={excerpt}
                      style={{
                        color: '#64748b',
                        borderLeft: '2px solid #e2e8f0',
                        paddingLeft: 8,
                        marginTop: 4,
                      }}
                    >
                      {excerpt}
                    </div>
                  ))}
                </li>
              ))}
            </ol>
          )}
        </div>

        {/* 分类 / 标签 */}
        <div
          style={{
            marginTop: 24,
            paddingTop: 12,
            borderTop: '1px solid #f1f5f9',
            fontSize: 12,
            color: '#94a3b8',
            display: 'flex',
            gap: 8,
            flexWrap: 'wrap',
            alignItems: 'center',
          }}
        >
          <span>{t('domainKnowledge.wikiReader.categories')}</span>
          {page.category && <Tag color="blue">{page.category}</Tag>}
          {(page.tags || []).map((tag) => (
            <Tag key={tag}>{tag}</Tag>
          ))}
        </div>
      </div>

      {/* 右栏：信息框（维基百科 infobox）+ 本页关系小地图（1-hop ego） */}
      {!page.tableData && <div style={{ width: 240, flex: '0 0 240px', display: 'grid', gap: 12 }}>
        <div
          style={{
            border: '1px solid #e2e8f0',
            borderRadius: 10,
            background: '#fbfdff',
            fontSize: 13,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              padding: '8px 12px',
              background: '#eff6ff',
              color: '#0b2b5c',
              fontWeight: 600,
              fontSize: 13,
            }}
          >
            {t('domainKnowledge.wikiReader.infobox')}
          </div>
          <div style={{ padding: '10px 12px', display: 'grid', gap: 8 }}>
            <InfoRow label={t('domainKnowledge.wikiReader.field.type')} value={t(roleLabelKey(role))} />
            {page.category && <InfoRow label={t('domainKnowledge.wikiReader.field.category')} value={page.category} />}
            {(page.aliases || []).length > 0 && (
              <InfoRow label={t('domainKnowledge.wikiReader.field.aliases')} value={(page.aliases || []).join('、')} />
            )}
            {(page.tags || []).length > 0 && (
              <InfoRow label={t('domainKnowledge.wikiReader.field.tags')} value={(page.tags || []).join('、')} />
            )}
            <InfoRow
              label={t('domainKnowledge.wikiReader.field.sources')}
              value={t('domainKnowledge.wikiReader.pageCount', { count: references.length })}
            />
            {(page.whyVisit || catalogPage?.whyVisit) && (
              <InfoRow
                label={t('domainKnowledge.wikiReader.field.whyVisit')}
                value={page.whyVisit || catalogPage?.whyVisit || ''}
              />
            )}
            {(page.limitations || []).length > 0 && (
              <InfoRow
                label={t('domainKnowledge.wikiReader.field.limitations')}
                value={(page.limitations || []).join('；')}
              />
            )}
          </div>
        </div>

        {/* 本页关联：右栏是 240px 的窄栏，画图必然压叠 —— 这里用分组列表（图谱 Tab 才画图）。
            分组顺序「页间链接 → 正文提及 → 链入本页 → 源文档」与图谱页完全一致。 */}
        <WikiLinkGroups
          groups={linkGroups}
          onOpenPage={onOpenPage}
          onOpenDocument={(documentId) => {
            const ref = references.find((r) => r.documentId === documentId);
            if (ref) openPageReference(ref);
            else openReference(documentId, documentId, '');
          }}
        />
      </div>}
      {viewer}
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 8 }}>
      <span style={{ color: '#94a3b8', flex: '0 0 62px' }}>{label}</span>
      <span style={{ color: '#334155', flex: 1, wordBreak: 'break-word' }}>{value}</span>
    </div>
  );
}
