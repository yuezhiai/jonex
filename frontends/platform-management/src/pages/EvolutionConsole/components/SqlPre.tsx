/** SQL / 长文本块（与 LogDetailModal 的局部实现同款样式，演化台自用副本）。
 * maxHeight 默认 240（不改既有调用方行为）；长日志/清单按需放宽。 */
export default function SqlPre({ text, maxHeight = 240 }: { text: string; maxHeight?: number }) {
  return (
    <pre
      style={{
        margin: 0,
        background: '#f8fafc',
        padding: 12,
        borderRadius: 8,
        fontSize: 12,
        maxHeight,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      }}
    >
      {text}
    </pre>
  );
}
