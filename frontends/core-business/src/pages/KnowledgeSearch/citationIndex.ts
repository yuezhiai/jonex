import type { KnowledgeReference, KnowledgeReferenceLocation } from '@/types/knowledgeSearch';

/** i18n 翻译函数的最小签名（只取本模块用到的形态） */
type Translator = (key: string, opts?: Record<string, unknown>) => string;

/** 引用角标 class，渲染层据此区分「引用角标」与普通上标 */
export const CITE_BADGE_CLASS = 'jonex-cite-badge';

/**
 * [jonex] 把正文里的 `[n]`（单个引用）或 `[n-m]`（连续引用范围）注入为引用角标标签，
 * 交给 XMarkdown 的 `components.sup` 渲染成可点击圆标；范围 `[n-m]` 注入 n、横杠、m
 * 三个节点，使起止编号各自独立成圆标。
 *
 * 作用在 **markdown 源码字符串**上而非 hast 树：`@ant-design/x-markdown` 基于 marked，
 * 不提供 rehype 插件入口（2026-10-16 接入 Ant Design X）。本函数取代了原先的
 * `rehypeCitationBadge`，正则与产出结构保持同口径。
 *
 * 代码区段（``` 围栏 / ` 行内）内的编号**不注入**：markdown 渲染器不会把代码里的 HTML
 * 当标签解析，注入进去只会变成可见的尖括号源码。围栏允许**未闭合到结尾** —— 流式输出
 * 期间围栏是逐步到达的，闭合前若按普通文本处理，代码块里会先冒出角标再变回去。
 *
 * 放在本模块（而非组件）是为了让角标切分逻辑脱离 React 依赖、可被单测直接覆盖。
 */
export function injectCitationBadges(md: string): string {
  if (!md) return '';
  // 在函数内建正则：带 `g` 的模块级正则若被 `test()`/`exec()` 复用它处会残留 lastIndex
  // （同文件 `MARKDOWN_BLOCK_PATTERNS` 的注释），而 split/replace 虽会重置，仍不值得留这个坑。
  const codeSegment = /((?:```|~~~)[\s\S]*?(?:```|~~~|$)|`[^`\n]*`)/g;
  const citePattern = /\[(\d+)(?:-(\d+))?\]/g;
  const badge = (num: string) => `<sup class="${CITE_BADGE_CLASS}" data-cite-index="${num}">${num}</sup>`;

  // split 带捕获组时，捕获到的代码区段落在奇数下标，偶数下标是需要处理的正文
  return md
    .split(codeSegment)
    .map((segment, index) => {
      if (index % 2 === 1) return segment;
      return segment.replace(citePattern, (_whole, start: string, end?: string) =>
        end ? `${badge(start)}<sup>-</sup>${badge(end)}` : badge(start),
      );
    })
    .join('');
}

/** 秒 → mm:ss（与 SessionCard 卡片内时间锚点展示同口径） */
export function formatTimestamp(sec?: number | null): string {
  if (sec == null || Number.isNaN(sec)) return '';
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

/**
 * [jonex] 解析答案内联引用角标的编号（1-based）。
 *
 * 编号来源优先级：rehype 插件下传的 `data-cite-index`（字符串）→ 角标文本子节点。
 * 契约：答案里第 n 个角标 ⇒ `references[n - 1]`（后端 `normalize_citations` /
 * `normalize_evidence_handles` 已按 `final_references` 首现下标 +1 重排）。
 *
 * 非法输入（`'-'`、空串、`'a'`、undefined 等）返回 null，调用方据此把角标降级为纯展示。
 */
export function parseCiteIndex(raw: unknown): number | null {
  const text = Array.isArray(raw) ? raw[0] : raw;
  if (text == null) return null;
  const s = String(text).trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/** location 是否带「可定位锚点」——`document` 级等无锚点的位置定位不了原文。 */
function hasLocator(loc: KnowledgeReferenceLocation): boolean {
  return (
    loc.page_no != null ||
    loc.time_start != null ||
    loc.time_end != null ||
    loc.image_idx != null ||
    loc.chunk_index != null
  );
}

/**
 * 取引用的「首选定位」——与「关联原文」卡片「查看原文」按钮同口径。
 *
 * 一条引用（尤其是本体路径按文档聚合的那类）可能带多个 location，**首个带正文的
 * location 才是详情面板展示、答案实际引用的那一段**，定位必须跟着它走：
 * 若改按「首个 page location」回落，图表/表格/后页引用会被拽到文档首页附近的
 * 无关正文（实测 2026-10-19「美国时薪同比分析」：引的是第 3 页图表，却跳到第 2 页
 * 的正文）。因此顺序是：
 *
 * 1. 纯图片文档：优先可直开的图片 location；
 * 2. 首个带正文且可定位的 location（= 展示正文所在处）；
 * 3. 时间锚点 → 页码 → 首个 location（无正文/无锚点的旧数据回落）。
 *
 * 抽成纯函数是为了让内联角标、引用详情面板、卡片按钮三处共用同一套规则，
 * 避免口径漂移（此前该三元表达式内联在卡片 onClick 里）。
 */
export function preferredLocation(ref: KnowledgeReference): KnowledgeReferenceLocation | undefined {
  const locs = ref.locations || [];
  if (locs.length === 0) return undefined;
  if (ref.media_type === 'image') {
    const imageLoc = locs.find((l) => l.type === 'image' && l.asset_url && l.image_idx != null);
    return imageLoc || locs[0];
  }
  const snippet = snippetLocation(ref);
  if (snippet && hasLocator(snippet)) return snippet;
  return (
    locs.find((l) => l.type === 'timestamp' && l.time_start != null) ||
    locs.find((l) => l.type === 'page' && l.page_no != null) ||
    locs[0]
  );
}

/**
 * 命中片段正文所在的 location —— 规则是「**首个带非空 text 的 location**」。
 *
 * 与「关联原文」卡片的 snippet 同口径。此前该规则内联在卡片里，而引用详情面板另按
 * `preferredLocation(...).text` 取正文，多 location 引用下会分叉：一条引用同时有不带
 * 正文的页码 location 和带正文的表格 location 时，卡片显示正文、面板却显示
 * 「该引用暂无命中片段原文」。抽成同一个函数后两处不可能再漂移。
 *
 * ⚠️ 与 `preferredLocation` 职责不同、**不可互换**：本函数回答「正文在哪」，
 * `preferredLocation` 回答「点『定位原文』该落到哪」（图片文档优先可直开的图片 location）。
 */
export function snippetLocation(ref: KnowledgeReference): KnowledgeReferenceLocation | undefined {
  return (ref.locations || []).find((l) => !!l.text);
}

/** md 扩展名：老口径，不看内容 */
const isMarkdownFile = (fileName: string) => /\.(md|markdown)$/i.test(fileName || '');

/**
 * 块级结构判据。只认行首形态，**刻意不收** `**粗体**` / `` `代码` `` / `> 引用` 这类行内语法：
 * 它们换不来排版收益，却把含 `_`、`*` 的纯文本拖进 markdown 解析器。
 * `MarkdownContent` 没有 `white-space: pre-wrap`，误判的代价是**换行被折成空格**，所以宁窄勿宽。
 *
 * 无 `g` 标志：带 `g` 会让 `lastIndex` 在多次 `test()` 之间残留，判据时真时假。
 */
const MARKDOWN_BLOCK_PATTERNS: RegExp[] = [
  /^ {0,3}#{1,6} /m, // 标题（`#` 后必须跟空格，`#话题` 不算）
  /^ {0,3}\|.*\|\s*$/m, // 表格行（行首行尾都是竖线才认）
  /^ {0,3}[-*+] /m, // 无序列表
  /^ {0,3}\d+\. /m, // 有序列表（只认 `1.` 不认 `1)`：后者在中文正文里太常见）
  /^ {0,3}```/m, // 代码围栏
];

/**
 * [jonex] 命中片段是否该走 `MarkdownContent`。
 *
 * 此前只看扩展名，pdf / office 片段哪怕含 `## 标题` 也只能按纯文本显示，与 md 引用观感割裂；
 * 这里对非 md 文件改按**内容**判断，纯文本一律原样输出。
 *
 * 与 `snippetLocation` 同住本模块：两个展示点（「关联原文」卡片 / 引用详情面板）共用同一份判据。
 */
export function isMarkdownSnippet(fileName: string, text: string): boolean {
  if (isMarkdownFile(fileName)) return true;
  if (!text) return false;
  return MARKDOWN_BLOCK_PATTERNS.some((p) => p.test(text));
}

/**
 * 定位 → 位置文案（引用详情面板「位置」行）。
 *
 * 页码保持既有口径：后端 `page_no` 为 0-based（MinerU page_idx），展示层 +1。
 * 无法产出有效文案（如 page 类型但 `page_no` 为空）返回 null，调用方不渲染该行。
 */
export function describeLocation(loc: KnowledgeReferenceLocation | undefined, t: Translator): string | null {
  if (!loc) return null;
  switch (loc.type) {
    case 'chunk':
      return loc.chunk_index != null ? t('knowledgeSearch.citationChunk', { index: loc.chunk_index }) : null;
    case 'page':
      return loc.page_no != null ? t('knowledgeSearch.docPage', { page: loc.page_no + 1 }) : null;
    case 'timestamp': {
      if (loc.time_start == null) return null;
      const start = formatTimestamp(loc.time_start);
      if (!start) return null;
      const end = loc.time_end != null ? formatTimestamp(loc.time_end) : '';
      return end ? `${start} - ${end}` : start;
    }
    case 'image': {
      if (loc.image_idx == null) return null;
      const image = t('knowledgeSearch.citationImage', { index: loc.image_idx });
      return loc.page_no != null ? `${image} · ${t('knowledgeSearch.docPage', { page: loc.page_no + 1 })}` : image;
    }
    case 'document':
      return t('knowledgeSearch.citationDocument');
    case 'char':
      return t('knowledgeSearch.citationFragment');
    default:
      return null;
  }
}
