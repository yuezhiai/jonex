/** Add layout breaks only when the answer already contains the complete, ordered section labels. */
export function formatWikiAnswer(content: string): string {
  if (!content || /```|~~~/.test(content)) return content;
  const markers = [...content.matchAll(/(^|\n|(?<=[。.!?])\s+)(Evidence|Interpretation|Conclusion|证据|解读|解释|结论)\s*[:：]/g)];
  const kinds = markers.map((m) => ['Evidence', '证据'].includes(m[2]) ? 'evidence'
    : ['Conclusion', '结论'].includes(m[2]) ? 'conclusion' : 'interpretation');
  if (markers.length !== 3 || kinds.join(',') !== 'evidence,interpretation,conclusion'
      || content.slice(0, markers[0].index).trim()) return content;
  let formatted = content;
  for (let i = markers.length - 1; i >= 0; i--) {
    const marker = markers[i];
    const at = marker.index!;
    const label = marker[0].slice(marker[1].length);
    formatted = formatted.slice(0, at) + `\n\n### ${label}\n\n` + formatted.slice(at + marker[0].length);
  }
  return formatted.trim();
}
