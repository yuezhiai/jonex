/**
 * [jonex] Wiki 阅读界面（kb_type=jonex_wiki 的编译结果 Tab）。
 *
 * 数据来源三条（都与检索链路解耦）：
 * - GET  /knowledge-base/wiki/outline        目录（LLM 起草/调整，落 knowledge_base.wiki_outlines）+ 页面清单
 * - POST /knowledge-base/wiki/outline/draft  AI 起草目录
 * - POST /knowledge-base/wiki/outline/adjust AI 按自然语言调整目录
 * - GET  /knowledge-base/parse-results/wiki-page  单页正文 + 页面级 References
 *
 * 展示模型（用户确认口径）：
 *   首页（主题卡片） → 主题页（有序目录，像课本） → 条目页（维基式文章 +
 *   蓝字 wikilink + 底部 References 溯源到源文档）
 * 不做红链：无法解析的链接降级为纯文本。目录只影响阅读体验，不参与检索。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Breadcrumb, Button, Input, Modal, Result, Space, Spin, Tooltip, message } from 'antd';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  HomeOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useKbPermission } from '@/hooks/useKbPermission';
import type { WikiBacklinkResult, WikiOutlineNode, WikiOutlineView, WikiPageContent } from '@/types/domainKnowledge';
import {
  adjustWikiOutline,
  draftWikiOutline,
  getWikiBacklinks,
  getWikiOutline,
  getWikiReaderPage,
} from '@/api/wikiReader';
import WikiOutlineRail from './WikiOutlineRail';
import WikiHomeView from './WikiHomeView';
import WikiTopicView from './WikiTopicView';
import WikiArticleView from './WikiArticleView';

type WikiView = { kind: 'home' } | { kind: 'topic'; nodeId: string } | { kind: 'article'; path: string };

interface WikiReaderShellProps {
  kbId: string;
  /**
   * 文档级阅读视图（文档详情页的「Wiki 界面」）：页面清单与目录都限定在该文档内。
   * 目录本身是**知识库级**的（LLM 起草、落库、带版本），这里只做过滤投影：
   * 文档级不提供「生成/调整目录」，改成一句说明。
   */
  documentId?: string;
  /** 知识库资产页由外层统一滚动；目录仍独立滚动。 */
  scrollHeight?: number;
}

/** 目录中的页面位置（面包屑 / 「所在章节」用）。 */
interface PageLocation {
  topicId: string;
  topicTitle: string;
  trail: string[];
}

export default function WikiReaderShell({ kbId, documentId = '', scrollHeight }: WikiReaderShellProps) {
  const { t } = useTranslation();
  const { canWrite: kbCanWrite } = useKbPermission(kbId);
  // 文档级：目录是知识库级的，此处只读（要改目录请去知识库级的「Wiki 界面」）
  const readOnly = !!documentId;
  const canWrite = kbCanWrite && !readOnly;

  const [data, setData] = useState<WikiOutlineView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const loadSequence = useRef(0);
  const currentScope = useRef(`${kbId}:${documentId}`);
  currentScope.current = `${kbId}:${documentId}`;
  const [adjustOpen, setAdjustOpen] = useState(false);
  const [adjustText, setAdjustText] = useState('');
  const [query, setQuery] = useState('');

  // 导航（带前进/后退栈；Wiki 是三段式阅读，读者会频繁来回跳）
  const [history, setHistory] = useState<WikiView[]>([{ kind: 'home' }]);
  const [cursor, setCursor] = useState(0);
  const view = history[cursor];

  const [page, setPage] = useState<WikiPageContent | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [pageError, setPageError] = useState('');
  // 链入本页（反向链接）：条目页列表 + ego 图共用一次请求
  const [backlinks, setBacklinks] = useState<WikiBacklinkResult | null>(null);
  const [backlinksLoading, setBacklinksLoading] = useState(false);
  const mainRef = useRef<HTMLDivElement | null>(null);

  const loadView = useCallback(
    async (silent = false) => {
      if (!kbId) return;
      const request = ++loadSequence.current;
      const scope = `${kbId}:${documentId}`;
      if (!silent) setLoading(true);
      try {
        const res = await getWikiOutline(kbId, documentId || undefined);
        if (request !== loadSequence.current || scope !== currentScope.current) return;
        setData(res);
        setLoadError('');
      } catch (err) {
        if (request === loadSequence.current && scope === currentScope.current) setLoadError((err as Error)?.message || t('common.loadFailed'));
      } finally {
        if (!silent && request === loadSequence.current && scope === currentScope.current) setLoading(false);
      }
    },
    [kbId, documentId, t],
  );

  useEffect(() => {
    void loadView();
    return () => { ++loadSequence.current; };
  }, [loadView]);

  useEffect(() => {
    setData(null);
    setHistory([{ kind: 'home' }]);
    setCursor(0);
    setPage(null);
    setBacklinks(null);
  }, [kbId, documentId]);

  const pages = useMemo(() => data?.pages || [], [data]);
  const outline = data?.outline || null;
  const pagesByPath = useMemo(() => new Map(pages.map((p) => [p.path, p])), [pages]);

  // 目录 → 页面位置索引（主题 / 小节链路）
  const pageLocations = useMemo(() => {
    const map = new Map<string, PageLocation>();
    const walk = (node: WikiOutlineNode, topicId: string, topicTitle: string, trail: string[]) => {
      const nextTrail = node.kind === 'topic' || node.kind === 'unclassified' ? trail : [...trail, node.title];
      for (const entry of node.entries || []) {
        if (!map.has(entry.path)) {
          map.set(entry.path, { topicId, topicTitle, trail: nextTrail });
        }
      }
      for (const child of node.children || []) walk(child, topicId, topicTitle, nextTrail);
    };
    for (const topic of outline?.topics || []) {
      walk(
        topic,
        topic.id,
        topic.kind === 'unclassified' ? t('domainKnowledge.wikiReader.unclassified') : topic.title,
        [],
      );
    }
    return map;
  }, [outline, t]);

  const topicById = useMemo(() => {
    const map = new Map<string, WikiOutlineNode>();
    const walk = (node: WikiOutlineNode) => {
      map.set(node.id, node);
      for (const child of node.children || []) walk(child);
    };
    for (const topic of outline?.topics || []) walk(topic);
    return map;
  }, [outline]);

  const searchResults = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return [];
    return pages.filter((p) => {
      const haystack = [p.title, p.description, p.category, ...(p.aliases || []), ...(p.tags || [])]
        .join(' ')
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [pages, query]);

  // ── 导航 ──
  const navigate = useCallback(
    (next: WikiView) => {
      setHistory((prev) => [...prev.slice(0, cursor + 1), next]);
      setCursor((prev) => prev + 1);
    },
    [cursor],
  );

  const openPage = useCallback(
    (path: string) => {
      if (!path) return;
      // 重复点击当前页不产生历史栈噪音（后退键语义保持「回到上一处」）
      if (view.kind === 'article' && view.path === path) return;
      navigate({ kind: 'article', path });
    },
    [navigate, view],
  );

  // 条目页正文加载
  useEffect(() => {
    if (view.kind !== 'article') {
      setPage(null);
      setPageError('');
      return;
    }
    let alive = true;
    setPage(null);
    setPageLoading(true);
    setPageError('');
    getWikiReaderPage(kbId, view.path)
      .then((res) => {
        if (alive) setPage(res);
      })
      .catch((err) => {
        if (alive) setPageError((err as Error)?.message || t('common.loadFailed'));
      })
      .finally(() => {
        if (alive) setPageLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [view, kbId, t]);

  // 链入本页（反向链接）：与正文并行取一次，条目页的列表与 ego 图共用
  useEffect(() => {
    if (view.kind !== 'article') {
      setBacklinks(null);
      return;
    }
    let alive = true;
    setBacklinks(null);
    setBacklinksLoading(true);
    getWikiBacklinks(kbId, view.path, documentId || undefined)
      .then((res) => {
        if (alive) setBacklinks(res);
      })
      .catch(() => {
        if (alive) setBacklinks(null);
      })
      .finally(() => {
        if (alive) setBacklinksLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [view, kbId, documentId]);

  // 视图切换回到顶部（长文章 + 左侧目录，不回到顶部会「跳在原地」）
  useEffect(() => {
    const main = mainRef.current;
    if (scrollHeight && main) {
      const host = main.closest<HTMLElement>('[data-wiki-scroll]');
      if (host && host.scrollTop > 0) {
        const top = main.getBoundingClientRect().top - host.getBoundingClientRect().top + host.scrollTop - 46;
        host.scrollTo({ top });
      }
    } else {
      main?.scrollTo?.({ top: 0 });
      window.scrollTo({ top: 0, behavior: 'auto' });
    }
  }, [view]);

  // ── 目录生成 / 调整 ──
  const handleDraft = useCallback(async () => {
    setBusy(true);
    try {
      const res = await draftWikiOutline(kbId, {
        unclassifiedTitle: t('domainKnowledge.wikiReader.unclassified'),
      });
      setData(res);
      message.success(t('domainKnowledge.wikiReader.draftDone'));
    } catch (err) {
      message.error((err as Error)?.message || t('common.operationFailed'));
    } finally {
      setBusy(false);
    }
  }, [kbId, t]);

  const handleAdjust = useCallback(async () => {
    const instruction = adjustText.trim();
    if (!instruction) return;
    setBusy(true);
    try {
      const res = await adjustWikiOutline(kbId, instruction, {
        expectedVersion: data?.outlineVersion,
        unclassifiedTitle: t('domainKnowledge.wikiReader.unclassified'),
      });
      setData(res);
      setAdjustOpen(false);
      setAdjustText('');
      message.success(t('domainKnowledge.wikiReader.adjustDone'));
    } catch (err) {
      message.error((err as Error)?.message || t('common.operationFailed'));
    } finally {
      setBusy(false);
    }
  }, [adjustText, kbId, data?.outlineVersion, t]);

  // ── 面包屑 ──
  const breadcrumbItems = useMemo(() => {
    const items: { title: React.ReactNode }[] = [
      {
        title: (
          <a onClick={() => navigate({ kind: 'home' })}>
            <HomeOutlined /> {t('domainKnowledge.wikiReader.home')}
          </a>
        ),
      },
    ];
    if (view.kind === 'topic') {
      const node = topicById.get(view.nodeId);
      if (node)
        items.push({ title: node.kind === 'unclassified' ? t('domainKnowledge.wikiReader.unclassified') : node.title });
    }
    if (view.kind === 'article') {
      const location = pageLocations.get(view.path);
      if (location) {
        items.push({
          title: <a onClick={() => navigate({ kind: 'topic', nodeId: location.topicId })}>{location.topicTitle}</a>,
        });
        if (location.trail.length > 0) items.push({ title: location.trail.join(' / ') });
      }
      items.push({ title: page?.title || pagesByPath.get(view.path)?.title || view.path });
    }
    return items;
  }, [view, topicById, pageLocations, page, pagesByPath, navigate, t]);

  const railMeta = useMemo(() => {
    if (!data) return '';
    const parts = [t('domainKnowledge.wikiReader.pageCount', { count: data.pageCount })];
    if (data.outlineVersion > 0) {
      parts.push(t('domainKnowledge.wikiReader.outlineVersion', { version: data.outlineVersion }));
    }
    if (data.generatedAt) {
      parts.push(new Date(data.generatedAt).toLocaleDateString());
    }
    // 过期提示在栏底也留一句：读者可能停在条目页，看不到首页的横幅
    if (data.stale) parts.push(t('domainKnowledge.wikiReader.staleShort'));
    parts.push(t('domainKnowledge.wikiReader.outlineOnlyHint'));
    return parts.join(' · ');
  }, [data, t]);

  if (loading) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  if (loadError) {
    return (
      <Result
        status="warning"
        title={t('common.loadFailed')}
        subTitle={loadError}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => loadView()}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    // 知识库资产页统一由面板滚动，目录吸顶且独立滚动；文档页保持内部滚动。
    <div style={{ display: 'flex', alignItems: 'stretch', height: scrollHeight ? 'auto' : '100%', minHeight: scrollHeight || 0 }}>
      <div style={{ width: 268, flexShrink: 0, ...(scrollHeight ? { position: 'sticky', top: 46, height: scrollHeight, alignSelf: 'flex-start' } : { height: '100%' }) }}>
        <WikiOutlineRail
        pages={pages}
        outline={outline}
        activePath={view.kind === 'article' ? view.path : ''}
        activeTopicId={view.kind === 'topic' ? view.nodeId : ''}
        onOpenHome={() => navigate({ kind: 'home' })}
        onOpenTopic={(nodeId) => (nodeId === 'home' ? navigate({ kind: 'home' }) : navigate({ kind: 'topic', nodeId }))}
        onOpenPage={openPage}
        query={query}
        onQueryChange={setQuery}
        searchResults={searchResults}
        metaText={railMeta}
        />
      </div>

      <div
        ref={mainRef}
        style={{
          flex: 1,
          minWidth: 0,
          height: scrollHeight ? 'auto' : '100%',
          boxSizing: 'border-box',
          overflowY: scrollHeight ? 'visible' : 'auto',
          padding: '12px 20px 32px',
        }}
      >
        {/* 工具条：面包屑 + 前进/后退 + 目录操作 */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            marginBottom: 14,
            flexWrap: 'wrap',
          }}
        >
          <Space size={4}>
            <Tooltip title={t('domainKnowledge.wikiReader.back')}>
              <Button
                size="small"
                type="text"
                icon={<ArrowLeftOutlined />}
                disabled={cursor === 0}
                onClick={() => setCursor((prev) => Math.max(0, prev - 1))}
              />
            </Tooltip>
            <Tooltip title={t('domainKnowledge.wikiReader.forward')}>
              <Button
                size="small"
                type="text"
                icon={<ArrowRightOutlined />}
                disabled={cursor >= history.length - 1}
                onClick={() => setCursor((prev) => Math.min(history.length - 1, prev + 1))}
              />
            </Tooltip>
          </Space>
          <Breadcrumb items={breadcrumbItems} style={{ flex: 1, fontSize: 13 }} />
          {/* 文档级：目录是知识库级的投影，这里只读 */}
          {readOnly && (
            <span style={{ fontSize: 12, color: '#94a3b8' }}>
              {t('domainKnowledge.wikiReader.docScopedHint')}
            </span>
          )}
          {/* 首页且无目录时，引导按钮已在页面正中，工具条不重复出现 */}
          {canWrite && (!!outline || view.kind !== 'home') && (
            <Space size={8}>
              {outline && (
                <Button size="small" icon={<RobotOutlined />} loading={busy} onClick={() => setAdjustOpen(true)}>
                  {t('domainKnowledge.wikiReader.adjust')}
                </Button>
              )}
              <Tooltip title={t('domainKnowledge.wikiReader.regenerateConfirm')}>
                <Button size="small" icon={<ThunderboltOutlined />} loading={busy} onClick={handleDraft}>
                  {outline ? t('domainKnowledge.wikiReader.regenerate') : t('domainKnowledge.wikiReader.generate')}
                </Button>
              </Tooltip>
            </Space>
          )}
        </div>

        {busy && (
          <div style={{ fontSize: 12, color: '#3b82f6', marginBottom: 10 }}>
            <Spin size="small" /> {t('domainKnowledge.wikiReader.llmBusy')}
          </div>
        )}

        {/* 主内容区 */}
        {view.kind === 'home' && (
          <WikiHomeView
            pages={pages}
            outline={outline}
            stale={data?.stale === true}
            canWrite={canWrite}
            busy={busy}
            generatedAt={data?.generatedAt || null}
            onOpenTopic={(nodeId) => navigate({ kind: 'topic', nodeId })}
            onOpenPage={openPage}
            onDraft={handleDraft}
            onAdjust={() => setAdjustOpen(true)}
          />
        )}

        {view.kind === 'topic' &&
          (() => {
            const node = topicById.get(view.nodeId);
            if (!node) {
              return (
                <Result
                  status="info"
                  title={t('domainKnowledge.wikiReader.topicMissing')}
                  extra={
                    <Button onClick={() => navigate({ kind: 'home' })}>{t('domainKnowledge.wikiReader.home')}</Button>
                  }
                />
              );
            }
            return (
              <WikiTopicView
                node={node}
                pagesByPath={pagesByPath}
                onOpenPage={openPage}
                onRequestAdjust={canWrite ? () => setAdjustOpen(true) : undefined}
              />
            );
          })()}

        {view.kind === 'article' && (
          <Spin spinning={pageLoading}>
            {pageError ? (
              <Result
                status="warning"
                title={t('common.loadFailed')}
                subTitle={pageError}
                extra={
                  <Button icon={<ReloadOutlined />} onClick={() => openPage(view.path)}>
                    {t('common.retry')}
                  </Button>
                }
              />
            ) : page ? (
              <WikiArticleView
                page={page}
                pages={pages}
                pagesByPath={pagesByPath}
                location={(() => {
                  const location = pageLocations.get(view.path);
                  return location
                    ? {
                        topicId: location.topicId,
                        topicTitle: location.topicTitle,
                        trailText: [location.topicTitle, ...location.trail].join(' / '),
                      }
                    : undefined;
                })()}
                backlinks={backlinks}
                backlinksLoading={backlinksLoading}
                onOpenPage={openPage}
                onOpenTopic={(nodeId) => navigate({ kind: 'topic', nodeId })}
              />
            ) : null}
          </Spin>
        )}
      </div>

      {/* AI 调整目录 */}
      <Modal
        open={adjustOpen}
        title={t('domainKnowledge.wikiReader.adjust')}
        onCancel={() => setAdjustOpen(false)}
        onOk={handleAdjust}
        confirmLoading={busy}
        okText={t('domainKnowledge.wikiReader.adjustSubmit')}
        cancelText={t('common.cancel')}
        okButtonProps={{ disabled: !adjustText.trim() }}
      >
        <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 8, lineHeight: 1.8 }}>
          {t('domainKnowledge.wikiReader.adjustHint')}
        </div>
        <Input.TextArea
          rows={4}
          value={adjustText}
          maxLength={2000}
          showCount
          // showCount 绝对定位在输入框下方一行，不占文档流，会压到弹窗 footer 的按钮
          style={{ marginBottom: 16 }}
          placeholder={t('domainKnowledge.wikiReader.adjustPlaceholder')}
          onChange={(e) => setAdjustText(e.target.value)}
        />
      </Modal>
    </div>
  );
}
