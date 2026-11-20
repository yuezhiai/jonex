/**
 * [jonex] Wiki 阅读界面的纯函数工具：wikilink 解析、正文分段、页内目录。
 *
 * 为什么不用旧 WikiPageBrowser 的 preprocessWikiLinks：
 * - 旧实现按 `{section}/{stem}`（openkb 口径）校验链接，而引擎页面正文里的
 *   wikilink 是 **canonical 路径**（`menus/2025-04-28.md`、`../source-evidence/x.md`），
 *   两套口径对不上，旧实现会把有效链接判成无效；
 * - 用户明确「不做红链」：无法解析的目标渲染为**纯文本**，不做任何提示色。
 *
 * 页面身份 = canonical 引擎路径，全链路（目录 / 链接 / 阅读 / References）统一。
 */
import type { WikiOutlineNode, WikiReaderPage } from '@/types/domainKnowledge';

/** 正文里的 wikilink：[[target]] / [[target|alias]] */
const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g;
/** 标准 markdown 内联链接：仅当目标是 .md 才当作 wiki 页链接处理，其余原样保留 */
const MDLINK_RE = /\[([^\]]+)\]\(([^)\s]+?)(?:\s+"[^"]*")?\)/g;

export interface WikiTocItem {
  level: number;
  text: string;
  id: string;
}

export interface WikiLinkResolution {
  markdown: string;
  /** 成功解析为站内页面的链接数（调试/统计用） */
  resolved: number;
  /** 成功解析到的 canonical 路径（去重顺序保留；用于「相关页面」与避免重复上色） */
  resolvedPaths: string[];
  /** 未能解析的目标（纯文本降级，不渲染红链） */
  unresolved: string[];
}

/** Leave hidden fragment names as text, while keeping article/table and original-document links. */
export function hideFragmentLinks(markdown: string, visiblePaths: Set<string>): string {
  return markdown.replace(/\[([^\]\n]+)\]\(#wiki\/([^\s)]+)\)/g, (original, label: string, encoded: string) => {
    try {
      return visiblePaths.has(decodeURIComponent(encoded)) ? original : label;
    } catch {
      return label;
    }
  });
}

/** 归一化相对路径（`../a/b.md` + `menus/x.md` → `a/b.md`）。 */
export function normalizeWikiPath(fromPath: string, target: string): string {
  if (!target.startsWith('./') && !target.startsWith('../')) return target;
  const baseDir = fromPath.includes('/') ? fromPath.slice(0, fromPath.lastIndexOf('/')) : '';
  const segments = `${baseDir}/${target}`.split('/');
  const stack: string[] = [];
  for (const segment of segments) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      stack.pop();
      continue;
    }
    stack.push(segment);
  }
  return stack.join('/');
}

/**
 * 构建页面解析器：把正文里的链接目标解析成 canonical 路径。
 *
 * 解析顺序（先精确后模糊，避免同名页面串页）：
 * 1. 精确路径；2. 补 `.md`；3. 同目录 stem 唯一命中；
 * 4. 全库 stem 唯一命中；5. 标题/别名唯一命中。
 */
export function makeWikiResolver(pages: WikiReaderPage[], currentPath: string) {
  const byPath = new Map(pages.map((p) => [p.path, p]));
  const byStem = new Map<string, string[]>();
  const byTitle = new Map<string, string[]>();
  for (const page of pages) {
    const stem = page.path.replace(/\.md$/, '').split('/').pop() || page.path;
    byStem.set(stem.toLowerCase(), [...(byStem.get(stem.toLowerCase()) || []), page.path]);
    for (const key of [page.title, ...(page.aliases || [])]) {
      const norm = (key || '').trim().toLowerCase();
      if (!norm) continue;
      byTitle.set(norm, [...(byTitle.get(norm) || []), page.path]);
    }
  }

  return (rawTarget: string): string | null => {
    const raw = (rawTarget || '').trim().split('#')[0].split('|')[0].trim();
    if (!raw) return null;
    const target = normalizeWikiPath(currentPath, raw).replace(/^\//, '');
    if (byPath.has(target)) return target;
    if (!target.endsWith('.md') && byPath.has(`${target}.md`)) return `${target}.md`;

    const stem = target.replace(/\.md$/, '').split('/').pop() || '';
    if (!stem) return null;
    const sameDir = target.includes('/')
      ? target.replace(/\.md$/, '')
      : `${currentPath.includes('/') ? currentPath.slice(0, currentPath.lastIndexOf('/')) : ''}/${stem}`;
    if (!sameDir.endsWith('.md') && byPath.has(`${sameDir}.md`)) return `${sameDir}.md`;

    const stemHits = byStem.get(stem.toLowerCase()) || [];
    if (stemHits.length === 1) return stemHits[0];
    const titleHits = byTitle.get(target.trim().toLowerCase()) || [];
    if (titleHits.length === 1) return titleHits[0];
    if (stemHits.length > 1 || titleHits.length > 1) return null; // 有歧义 → 不猜
    return null;
  };
}

function encodeWikiHref(path: string): string {
  return `#wiki/${encodeURIComponent(path)}`;
}

/**
 * 把正文里的 wikilink / 站内 md 链接转成应用内链接。
 * 解析不到的目标降级为纯文本（**不做红链**，这是用户明确要求）。
 */
export function resolveWikiLinks(markdown: string, resolve: (target: string) => string | null): WikiLinkResolution {
  let resolved = 0;
  const resolvedPaths: string[] = [];
  const unresolved: string[] = [];
  const remember = (path: string) => {
    resolved += 1;
    if (!resolvedPaths.includes(path)) resolvedPaths.push(path);
  };

  let out = (markdown || '').replace(WIKILINK_RE, (_match, target: string, alias?: string) => {
    const text = (alias || target || '').trim();
    const hit = resolve(target);
    if (hit) {
      remember(hit);
      return `[${text}](${encodeWikiHref(hit)})`;
    }
    unresolved.push((target || '').trim());
    return text; // 纯文本降级
  });

  out = out.replace(MDLINK_RE, (match, text: string, target: string) => {
    const href = (target || '').trim();
    // 跳过已生成的应用内链接 / 纯锚点 / 带协议的链接（http、mailto、data…）
    // —— 否则第二遍会把上一遍刚造出来的 `#wiki/xxx.md` 当普通 .md 链接再解析一次，
    //    解析不到就把链接降级成纯文本（**会静默吃掉链接**）。
    if (href.startsWith('#') || href.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(href)) {
      return match;
    }
    if (!/\.md($|[#?])/i.test(href)) return match; // 非 wiki 页链接（图片/附件）原样保留
    const hit = resolve(href);
    if (hit) {
      remember(hit);
      return `[${text}](${encodeWikiHref(hit)})`;
    }
    unresolved.push(href);
    return text;
  });

  return { markdown: out, resolved, resolvedPaths, unresolved };
}

/**
 * 首次提及自动链接（维基百科口径）：正文里第一次提到另一个页面的标题/别名时，
 * 把那处文字变成蓝链。
 *
 * 为什么需要它：引擎产物并不保证有 `[[wikilink]]`——真实样例里 69 页只有 1 页
 * 带显式 wikilink，但 18 页的正文提到了其他页面的标题。没有这一步，「相关概念
 * 用蓝色字体、点进去」在多数语料上会退化成普通文档。
 *
 * 克制规则（避免噪音/误链）：
 * - 每个目标页最多链一次（首次提及），每页最多 maxLinks 条；
 * - 名称长度门槛（≥3 个字符），过短易误伤；
 * - ASCII 名称要求词边界（SLA 不会命中 SLACK）；
 * - 标题行、代码块、行内代码、已有链接内一律不上色；
 * - 精确路径/wikilink 命中的目标不重复上色（调用方用 exclude 传入）。
 */
export function autoLinkMentions(
  markdown: string,
  options: {
    pages: WikiReaderPage[];
    currentPath: string;
    exclude?: Set<string>;
    maxLinks?: number;
  },
): { markdown: string; linked: string[] } {
  const { pages, currentPath, exclude, maxLinks = 8 } = options;
  const candidates = collectMentionCandidates(pages, currentPath);
  if (candidates.length === 0) return { markdown, linked: [] };

  const alternatives = candidates.map((c) => escapeRegExp(c.name));
  const pattern = new RegExp(alternatives.join('|'), 'gi');
  const byName = new Map(candidates.map((c) => [c.name.toLowerCase(), c.path]));
  const used = new Set<string>();
  const linked: string[] = [];

  let inFence = false;
  const lines = (markdown || '').replace(/\r\n/g, '\n').split('\n');
  // A page alias may equal an entire table header. Wrapping that row in a
  // link destroys GFM table syntax, so leave recognized table rows intact.
  const tableLines = new Set<number>();
  const divider = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$/;
  for (let index = 1; index < lines.length; index += 1) {
    if (!lines[index].includes('|') || !divider.test(lines[index]) || !lines[index - 1].includes('|')) continue;
    tableLines.add(index - 1);
    tableLines.add(index);
    for (let row = index + 1; row < lines.length && lines[row].includes('|') && lines[row].trim(); row += 1) {
      tableLines.add(row);
    }
  }
  const out = lines.map((line, index) => {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      return line;
    }
    if (inFence || tableLines.has(index) || /^\s*#{1,6}\s/.test(line)) return line;
    // 用捕获组 split：奇数位是「保护区」（已有链接 / 行内代码），不动
    return line
      .split(/(\[[^\]]*\]\([^)]*\)|`[^`]*`|!\[[^\]]*\]\([^)]*\))/)
      .map((segment, index) => {
        if (index % 2 === 1 || linked.length >= maxLinks) return segment;
        return segment.replace(pattern, (match) => {
          const path = byName.get(match.toLowerCase());
          if (!path || used.has(path) || exclude?.has(path) || linked.length >= maxLinks) {
            return match;
          }
          used.add(path);
          linked.push(path);
          return `[${match}](${encodeWikiHref(path)})`;
        });
      })
      .join('');
  });

  return { markdown: out.join('\n'), linked };
}

/** 候选提及名（页面标题 + 别名；过短的丢弃；长的排前面以优先匹配更具体的名字）。 */
export function collectMentionCandidates(
  pages: WikiReaderPage[],
  currentPath: string,
): { name: string; path: string }[] {
  const out: { name: string; path: string }[] = [];
  const seen = new Set<string>();
  for (const page of pages) {
    if (page.path === currentPath) continue;
    for (const raw of [page.title, ...(page.aliases || [])]) {
      const name = (raw || '').trim();
      if (!name || looksAmbiguous(name)) continue;
      const key = `${name.toLowerCase()}::${page.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ name, path: page.path });
    }
  }
  return out.sort((a, b) => b.name.length - a.name.length);
}

function looksAmbiguous(name: string): boolean {
  // 名称至少 3 个字符：中文 3 字（审计委员会）、ASCII 3 字符（SLA/KPI）都算有效概念名，
  // 更短的多为量词或缩写碎片，上色只会制造噪音。
  return name.length < 3;
}

function escapeRegExp(text: string): string {
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // ASCII 名称加词边界，避免命中更长单词的一部分
  return /^[\x20-\x7e]+$/.test(text) ? `(?<![A-Za-z0-9])${escaped}(?![A-Za-z0-9])` : escaped;
}

export interface WikiBodyParts {
  /** Summary 段（维基条目的导语） */
  lead: string;
  /** 正文（原 `## Content` + 其他自定义章节；已剥掉引擎固定外壳） */
  body: string;
  /** 被剥离的引擎外壳章节标题（Related pages / Backlinks / Sources） */
  droppedHeadings: string[];
}

const NO_SUMMARY_RE = /^_?No summary\.?_?$/i;
const DROPPED_HEADINGS = new Set(['related pages', 'backlinks', 'sources', '相关页面', '反向链接', '来源', '参考文献']);

/**
 * 拆解引擎页面正文：Summary → 导语；Content → 正文；
 * Related pages / Backlinks / Sources → 剥离（阅读界面用页面级
 * 相关页面 + 文档级 References 重新呈现，避免露出编译中间产物）。
 */
export function splitWikiBody(content: string): WikiBodyParts {
  const text = (content || '').replace(/\r\n/g, '\n');
  const leadParts: string[] = [];
  const bodyParts: string[] = [];
  const dropped: string[] = [];

  let current: 'lead' | 'body' | 'dropped' | null = 'lead';
  const lines = text.split('\n');
  for (const line of lines) {
    const heading = /^##\s+(.+?)\s*$/.exec(line);
    const h1 = /^#\s+(.+?)\s*$/.exec(line);
    if (h1) continue; // H1 = 页面标题，阅读界面自己渲染
    if (heading) {
      const title = heading[1].trim();
      const lower = title.toLowerCase();
      if (lower === 'summary' || title === '摘要') {
        current = 'lead';
        continue;
      }
      if (lower === 'content' || title === '正文') {
        current = 'body';
        continue;
      }
      if (DROPPED_HEADINGS.has(lower)) {
        current = 'dropped';
        dropped.push(title);
        continue;
      }
      current = 'body';
      bodyParts.push(line);
      continue;
    }
    if (current === 'lead') leadParts.push(line);
    else if (current === 'body') bodyParts.push(line);
  }

  const lead = leadParts
    .join('\n')
    .trim()
    .split('\n')
    .filter((line) => !NO_SUMMARY_RE.test(line.trim()))
    .join('\n')
    .trim();

  const body = bodyParts.join('\n').trim();
  return { lead: isRepeatedWikiLead(lead, body) ? '' : lead, body, droppedHeadings: dropped };
}

/** 展示侧摘录去重：保留完整正文，仅省略与正文开头相同的导语。 */
export function isRepeatedWikiLead(lead: string, body: string): boolean {
  const plain = (markdown: string) => markdown
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, target, label) => label || target)
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s*(?:#{1,6}\s+.*|[-*+]\s+|\d+[.)]\s+|>\s*)/gm, '')
    .replace(/[*_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const excerpt = plain(lead);
  return !!excerpt && plain(body).startsWith(excerpt);
}

/** 提取页内目录（h2~h4），并生成与正文渲染顺序一致的锚点 id。 */
export function extractToc(markdown: string): WikiTocItem[] {
  const items: WikiTocItem[] = [];
  let inFence = false;
  let seq = 0;
  for (const line of (markdown || '').replace(/\r\n/g, '\n').split('\n')) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    // 允许块引用内的标题（`> ## x`）：react-markdown 同样会渲染成标题，
    // 不匹配就会让锚点 id 与正文标题错位。
    const match = /^\s*>?\s*(#{2,4})\s+(.+?)\s*$/.exec(line);
    if (!match) continue;
    seq += 1;
    items.push({ level: match[1].length, text: match[2].trim(), id: `wiki-sec-${seq}` });
  }
  return items;
}

/** 递归统计目录节点下的条目数（目录树/主题卡片显示「这一章有多少页」）。 */
export function countEntries(node: WikiOutlineNode): number {
  const own = (node.entries || []).length;
  return own + (node.children || []).reduce((sum, child) => sum + countEntries(child), 0);
}

/** 页面正文分析结果（条目页与图谱页共用，保证两处「关联关系」完全一致）。 */
export interface WikiPageLinkAnalysis {
  leadMarkdown: string;
  bodyMarkdown: string;
  toc: WikiTocItem[];
  /** 显式 wikilink 解析出的目标路径 */
  explicitPaths: string[];
  /** 正文首次提及而被上色的目标路径（与正文蓝链同源） */
  mentionPaths: string[];
  /** 解析不到的目标（降级为纯文本，不做红链） */
  unresolved: string[];
}

/**
 * 一页正文的链接分析：分段 → 解析显式 wikilink → 首次提及自动链接 → 页内目录。
 *
 * 条目页（渲染正文）与图谱页（画 1-hop 关系）都要这套结果；抽成一个纯函数，
 * 避免两处各写一遍导致「图上的邻居」和「正文里的蓝字」对不上。
 */
export function analyzePageLinks(content: string, pages: WikiReaderPage[], currentPath: string): WikiPageLinkAnalysis {
  const resolve = makeWikiResolver(pages, currentPath);
  const parts = splitWikiBody(content || '');
  const leadResolved = resolveWikiLinks(parts.lead, resolve);
  const bodyResolved = resolveWikiLinks(parts.body, resolve);
  const explicit = new Set([...leadResolved.resolvedPaths, ...bodyResolved.resolvedPaths]);
  // 显式 wikilink 优先；剩余目标靠「首次提及」上色（引擎产物多数没有 [[ ]]）
  const leadAuto = autoLinkMentions(leadResolved.markdown, {
    pages,
    currentPath,
    exclude: explicit,
    maxLinks: 4,
  });
  const bodyAuto = autoLinkMentions(bodyResolved.markdown, {
    pages,
    currentPath,
    exclude: new Set([...explicit, ...leadAuto.linked]),
    maxLinks: 8,
  });
  return {
    leadMarkdown: leadAuto.markdown,
    bodyMarkdown: bodyAuto.markdown,
    toc: extractToc(bodyAuto.markdown),
    explicitPaths: [...explicit],
    mentionPaths: [...leadAuto.linked, ...bodyAuto.linked],
    unresolved: [...leadResolved.unresolved, ...bodyResolved.unresolved],
  };
}

/** 角色 → 展示文案 key（i18n 在组件里拼，这里只归一化角色）。 */
export function normalizeRole(role: string): 'article' | 'concept' | 'table' | 'source' | 'other' {
  const value = (role || '').toLowerCase();
  if (value === 'article' || value === 'concept' || value === 'table' || value === 'source') return value;
  return 'other';
}

/** 页面 role 的中文/英文短标签（用 locale key 由组件翻译；此处给出稳定 key 后缀）。 */
export function roleLabelKey(role: string): string {
  return `domainKnowledge.wikiReader.role.${normalizeRole(role)}`;
}
