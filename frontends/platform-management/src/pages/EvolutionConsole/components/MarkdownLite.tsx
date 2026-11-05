import React from 'react';

/** 行内渲染：`code` 与 **粗体**（其余纯文本）。 */
function inline(text: string, keyPrefix: string): React.ReactNode[] {
  const parts = text.split(/(`[^`]*`|\*\*[^*]+\*\*)/g).filter(Boolean);
  return parts.map((p, i) => {
    if (p.startsWith('`') && p.endsWith('`')) {
      return (
        <code key={`${keyPrefix}-${i}`} style={{ background: '#f4f4f5', padding: '0 4px', borderRadius: 4 }}>
          {p.slice(1, -1)}
        </code>
      );
    }
    if (p.startsWith('**') && p.endsWith('**')) {
      return <b key={`${keyPrefix}-${i}`}>{p.slice(2, -2)}</b>;
    }
    return <React.Fragment key={`${keyPrefix}-${i}`}>{p}</React.Fragment>;
  });
}

/** 极简 Markdown 渲染（不引入新依赖）：标题 / 列表 / 代码块 / 引用 / 分隔线 / 行内 code 与粗体。
 * 表格与链接不专门渲染（按普通文本行显示）——报告可读即可，求全请开 report.md 原文。 */
export default function MarkdownLite({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  const lines = (text || '').split(/\r?\n/);
  let code: string[] | null = null;
  let list: string[] = [];

  const flushList = (i: number) => {
    if (list.length) {
      out.push(
        <ul key={`ul-${i}`} style={{ margin: '4px 0', paddingLeft: 20 }}>
          {list.map((li, j) => (
            <li key={j}>{inline(li, `li-${i}-${j}`)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };

  lines.forEach((line, i) => {
    if (line.startsWith('```')) {
      if (code === null) {
        flushList(i);
        code = [];
      } else {
        out.push(
          <pre key={`pre-${i}`} style={{ background: '#f8fafc', padding: 12, borderRadius: 8, fontSize: 12, overflow: 'auto' }}>
            {code.join('\n')}
          </pre>,
        );
        code = null;
      }
      return;
    }
    if (code !== null) {
      code.push(line);
      return;
    }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
      list.push(line.replace(/^\s*[-*]\s+|^\s*\d+\.\s+/, ''));
      return;
    }
    flushList(i);
    if (!line.trim()) {
      out.push(<div key={`sp-${i}`} style={{ height: 6 }} />);
      return;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const sizes = [20, 18, 16, 14];
      out.push(
        <div key={`h-${i}`} style={{ fontWeight: 600, fontSize: sizes[h[1].length - 1], margin: '10px 0 4px' }}>
          {inline(h[2], `h-${i}`)}
        </div>,
      );
      return;
    }
    if (line.startsWith('> ')) {
      out.push(
        <blockquote key={`q-${i}`} style={{ borderLeft: '3px solid #d9d9d9', margin: '4px 0', paddingLeft: 10, color: '#595959' }}>
          {inline(line.slice(2), `q-${i}`)}
        </blockquote>,
      );
      return;
    }
    if (/^-{3,}$/.test(line.trim())) {
      out.push(<hr key={`hr-${i}`} />);
      return;
    }
    out.push(<p key={`p-${i}`} style={{ margin: '4px 0' }}>{inline(line, `p-${i}`)}</p>);
  });
  flushList(lines.length);
  if (code !== null) {
    out.push(
      <pre key="pre-open" style={{ background: '#f8fafc', padding: 12, borderRadius: 8, fontSize: 12 }}>
        {(code as string[]).join('\n')}
      </pre>,
    );
  }
  return <div style={{ fontSize: 13, lineHeight: 1.7 }}>{out}</div>;
}
