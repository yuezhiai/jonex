import React from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import { defaultSchema } from 'hast-util-sanitize';
import { PictureOutlined } from '@ant-design/icons';
import MarkdownTable, { markdownTableCell } from './MarkdownTable';
import WikiSourcePicture from './WikiSourcePicture';
import type { WikiSourceImage } from '@/types/domainKnowledge';


interface MarkdownContentProps {
  /** Markdown / GFM / 内联 HTML 源文本 */
  content: string;
  /** 图片占位文案（可选，默认「图片」） */
  imagePlaceholder?: string;
  /**
   * [jonex] 内部链接点击回调：href 以 `#wiki/` 开头时不再新开标签页，
   * 而是回调 canonical 路径（Wiki 阅读界面用它做应用内跳转）。
   * 不传时行为与旧版一致（所有链接 target=_blank）。
   */
  onInternalLink?: (target: string) => void;
  /**
   * [jonex] 依次挂到 h2~h4 上的锚点 id（页内目录跳转用）。
   * 顺序须与正文 h2~h4 出现顺序一致（由 wikiMarkdown.extractToc 生成）。
   */
  headingIds?: string[];
  images?: WikiSourceImage[];
  onOpenImageSource?: (image: WikiSourceImage) => void;
}

// 安全白名单：在 react-markdown 默认 schema 基础上，额外放行 HTML 表格相关标签（table/tr/td/th/br）
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'br'],
  attributes: {
    ...(defaultSchema.attributes ?? {}),
    table: ['border', 'width', 'cellspacing', 'cellpadding'],
    td: ['colspan', 'rowspan'],
    th: ['colspan', 'rowspan'],
  },
};

const headingStyle: React.CSSProperties = {
  color: '#0b2b5c',
  fontWeight: 600,
  margin: '0 0 10px',
  lineHeight: 1.5,
};
const headingSizes: Record<string, number> = { h1: 20, h2: 17, h3: 15, h4: 14, h5: 13, h6: 13 };

/** 内部链接前缀（wiki 阅读界面的应用内跳转；`#` 开头不含协议，可过 rehype-sanitize 白名单）。 */
export const WIKI_LINK_PREFIX = '#wiki/';

/** 行内代码：浅底玫红字的色块 */
const inlineCodeStyle: React.CSSProperties = {
  background: '#f1f5f9',
  borderRadius: 4,
  padding: '1px 6px',
  fontSize: 13,
  color: '#be185d',
};

/**
 * 围栏代码块里的 code：pre 本身已是深底浅字，这里必须把行内那套色块样式清干净。
 * 不清就会出现浅底压在深底上、玫红字压在浅底上 —— 即「白底红字」。
 */
const blockCodeStyle: React.CSSProperties = {
  background: 'transparent',
  color: 'inherit',
  padding: 0,
  borderRadius: 0,
  fontSize: 'inherit',
};

/** 围栏代码块的 pre 外壳（深底浅字） */
const preStyle: React.CSSProperties = {
  background: '#0f172a',
  color: '#e2e8f0',
  borderRadius: 8,
  padding: 12,
  fontSize: 13,
  lineHeight: 1.6,
  overflow: 'auto',
  margin: '0 0 12px',
};

/**
 * 子文档容器的外观：纯浅底圆角块，不加边框。
 * 底色比 blockquote 的 #f8fafc 深一档，两者叠在一起仍分得清。
 */
const embedStyle: React.CSSProperties = {
  background: '#f1f5f9',
  borderRadius: 6,
  padding: '10px 14px',
  margin: '0 0 12px',
};

/**
 * 当子文档渲染的围栏语言。JonexWiki 引擎只产这一个字面量
 * （retrieval.py:1187 / table_reader.py:1559），不认 `md`。
 *
 * ` ```text ` 是引擎的「保持来源字面」块（retrieval.py:1528），故意不在此列 ——
 * 它常把 markdown 围栏套在里面，整体按代码块显示才是它要的语义。
 */
const MARKDOWN_FENCE_LANG = /^language-markdown$/;

// 提到组件外，pre 里要按它这个身份认领自己的子节点（见下方 pre 覆盖）
const Code: Components['code'] = ({ children, node: _node, ...rest }) => (
  <code style={inlineCodeStyle} {...rest}>
    {children}
  </code>
);

/** 取 code 元素里的纯文本（react-markdown 的 children 可能是字符串或字符串数组） */
const toText = (node: React.ReactNode): string =>
  React.Children.toArray(node)
    .map((child) => (typeof child === 'string' || typeof child === 'number' ? String(child) : ''))
    .join('');

interface ComponentsOptions {
  images?: WikiSourceImage[];
  onOpenImageSource?: (image: WikiSourceImage) => void;
  /** 是否把 ```markdown 围栏当子文档渲染。子文档内部传 false，切断递归 */
  markdownFence: boolean;
  /**
   * 依次挂到 h2~h4 的锚点 id。子文档渲染【必须】不传：
   * extractToc 跳过围栏，围栏里的标题本就没有锚点，传了只会按渲染顺序抢走正文标题的 id。
   */
  headingIds?: string[];
  onInternalLink?: (target: string) => void;
  imagePlaceholder: string;
}

/** 顶层与子文档共用的渲染管线外壳 */
function MarkdownBody({ content, components }: { content: string; components: Components }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
      components={components}
    >
      {content}
    </ReactMarkdown>
  );
}

/**
 * 构建 components 映射。做成工厂是为了让 headingIds 的消费游标随每次渲染重置，
 * 并让子文档渲染能拿到一份「不再拆围栏、也不消费锚点」的映射。
 */
function buildComponents({
  markdownFence,
  headingIds,
  onInternalLink,
  imagePlaceholder,
  images,
  onOpenImageSource,
}: ComponentsOptions): Components {
  // 每次渲染重置游标：h2~h4 渲染顺序 = 正文出现顺序，与 headingIds 一一对应。
  let headingCursor = 0;
  const nextHeadingId = () => (headingIds ? headingIds[headingCursor++] : undefined);

  return {
    h1: ({ children }) => <h1 style={{ ...headingStyle, fontSize: headingSizes.h1 }}>{children}</h1>,
    h2: ({ children }) => (
      <h2 id={nextHeadingId()} style={{ ...headingStyle, fontSize: headingSizes.h2 }}>{children}</h2>
    ),
    h3: ({ children }) => (
      <h3 id={nextHeadingId()} style={{ ...headingStyle, fontSize: headingSizes.h3 }}>{children}</h3>
    ),
    h4: ({ children }) => (
      <h4 id={nextHeadingId()} style={{ ...headingStyle, fontSize: headingSizes.h4 }}>{children}</h4>
    ),
    h5: ({ children }) => <h5 style={{ ...headingStyle, fontSize: headingSizes.h5 }}>{children}</h5>,
    h6: ({ children }) => <h6 style={{ ...headingStyle, fontSize: headingSizes.h6 }}>{children}</h6>,
    p: ({ children }) => <p style={{ margin: '0 0 10px' }}>{children}</p>,
    strong: ({ children }) => <strong style={{ color: '#0b2b5c' }}>{children}</strong>,
    ul: ({ children }) => <ul style={{ margin: '0 0 10px', paddingLeft: 22 }}>{children}</ul>,
    ol: ({ children }) => <ol style={{ margin: '0 0 10px', paddingLeft: 22 }}>{children}</ol>,
    li: ({ children }) => <li style={{ marginBottom: 2 }}>{children}</li>,
    blockquote: ({ children }) => (
      <blockquote
        style={{
          margin: '0 0 12px',
          padding: '4px 12px',
          borderLeft: '3px solid #3b82f6',
          background: '#f8fafc',
          color: '#475569',
        }}
      >
        {children}
      </blockquote>
    ),
    a: ({ href, children }) => {
      const target = href || '';
      if (onInternalLink && target.startsWith(WIKI_LINK_PREFIX)) {
        const raw = target.slice(WIKI_LINK_PREFIX.length);
        let decoded = raw;
        try {
          decoded = decodeURIComponent(raw);
        } catch {
          /* 非法百分号编码：按原文处理 */
        }
        return (
          <a
            href={target}
            onClick={(event) => {
              event.preventDefault();
              if (decoded) onInternalLink(decoded);
            }}
            style={{ color: '#2563eb', cursor: 'pointer' }}
          >
            {children}
          </a>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer" style={{ color: '#3b82f6' }}>
          {children}
        </a>
      );
    },
    code: Code,
    pre: ({ children }) => {
      const codeEl = React.isValidElement(children)
        ? (children as React.ReactElement<{
            className?: string;
            children?: React.ReactNode;
            style?: React.CSSProperties;
          }>)
        : null;
      const owned = codeEl?.type === Code ? codeEl : null;

      // ```markdown → 当子文档渲染（源码直接铺开的话，表格只会显示成 | 和 ---）
      if (markdownFence && owned && MARKDOWN_FENCE_LANG.test(owned.props.className ?? '')) {
        const source = toText(owned.props.children).trim();
        if (source) {
          return (
            <div className="yx-md-embed" style={embedStyle}>
              <MarkdownBody
                content={source}
                components={buildComponents({ markdownFence: false, onInternalLink, imagePlaceholder, images, onOpenImageSource })}
              />
            </div>
          );
        }
      }

      return <pre style={preStyle}>{owned ? React.cloneElement(owned, { style: blockCodeStyle }) : children}</pre>;
    },
    // 表格与答案正文共用一套渲染（`@/components/MarkdownTable`）：带边框/斑马纹、
    // 窄容器可横向滚动，单元格是 ☑/□ 一类符号时换成可读色块
    table: ({ children }) => <MarkdownTable>{children}</MarkdownTable>,
    th: ({ node: _node, ...rest }) => <th {...rest} />,
    td: ({ node: _node, children, ...rest }) => (
      <td {...rest}>{markdownTableCell(children)}</td>
    ),
    img: ({ alt, src }) => {
      const id = typeof src === 'string' && src.startsWith('#wiki-image/') ? src.slice('#wiki-image/'.length) : '';
      const image = images?.find((item) => item.asset_id === id);
      return image ? <WikiSourcePicture image={image} onOpenSource={onOpenImageSource} /> : (
      <span style={{ color: '#94a3b8', fontSize: 12 }}>
        <PictureOutlined /> {alt || imagePlaceholder}
      </span>
    ); },
  };
}

/**
 * 统一 Markdown 渲染组件：remark-gfm（表格/任务列表/删除线）+ 样式定制。
 * 用于解析结果片段、文档正文等可能带格式的内容展示。
 *
 * ```markdown 围栏（JonexWiki 引擎的表格投影）会当子文档渲染成预览，不再显示源码。
 */
export default function MarkdownContent({
  content,
  imagePlaceholder = '图片',
  onInternalLink,
  headingIds,
  images,
  onOpenImageSource,
}: MarkdownContentProps) {
  return (
    <div style={{ fontSize: 14, lineHeight: 1.8, color: '#334155', wordBreak: 'break-word' }}>
      <MarkdownBody
        content={content}
        components={buildComponents({ markdownFence: true, headingIds, onInternalLink, imagePlaceholder, images, onOpenImageSource })}
      />
    </div>
  );
}
