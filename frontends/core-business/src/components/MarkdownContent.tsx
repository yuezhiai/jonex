import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import { defaultSchema } from 'hast-util-sanitize';
import { PictureOutlined } from '@ant-design/icons';

interface MarkdownContentProps {
  /** Markdown / GFM / 内联 HTML 源文本 */
  content: string;
  /** 图片占位文案（可选，默认「图片」） */
  imagePlaceholder?: string;
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

/**
 * 统一 Markdown 渲染组件：remark-gfm（表格/任务列表/删除线）+ 样式定制。
 * 用于解析结果片段、文档正文等可能带格式的内容展示。
 */
export default function MarkdownContent({ content, imagePlaceholder = '图片' }: MarkdownContentProps) {
  return (
    <div style={{ fontSize: 14, lineHeight: 1.8, color: '#334155', wordBreak: 'break-word' }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
        components={{
          h1: ({ children }) => <h1 style={{ ...headingStyle, fontSize: headingSizes.h1 }}>{children}</h1>,
          h2: ({ children }) => <h2 style={{ ...headingStyle, fontSize: headingSizes.h2 }}>{children}</h2>,
          h3: ({ children }) => <h3 style={{ ...headingStyle, fontSize: headingSizes.h3 }}>{children}</h3>,
          h4: ({ children }) => <h4 style={{ ...headingStyle, fontSize: headingSizes.h4 }}>{children}</h4>,
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
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer" style={{ color: '#3b82f6' }}>
              {children}
            </a>
          ),
          code: ({ children }) => (
            <code
              style={{
                background: '#f1f5f9',
                borderRadius: 4,
                padding: '1px 6px',
                fontSize: 13,
                color: '#be185d',
              }}
            >
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre
              style={{
                background: '#0f172a',
                color: '#e2e8f0',
                borderRadius: 8,
                padding: 12,
                fontSize: 13,
                lineHeight: 1.6,
                overflow: 'auto',
                margin: '0 0 12px',
              }}
            >
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <table
              style={{
                borderCollapse: 'collapse',
                margin: '0 0 12px',
                width: '100%',
                fontSize: 13,
              }}
            >
              {children}
            </table>
          ),
          th: ({ node: _node, ...rest }) => (
            <th
              style={{
                border: '1px solid #e2e8f0',
                background: '#f8fafc',
                padding: '6px 10px',
                fontWeight: 600,
                textAlign: 'left',
              }}
              {...rest}
            />
          ),
          td: ({ node: _node, ...rest }) => (
            <td style={{ border: '1px solid #e2e8f0', padding: '6px 10px' }} {...rest} />
          ),
          img: ({ alt }) => (
            <span style={{ color: '#94a3b8', fontSize: 12 }}>
              <PictureOutlined /> {alt || imagePlaceholder}
            </span>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
