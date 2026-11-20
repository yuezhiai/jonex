import type { WikiOutline, WikiOutlineNode, WikiReaderPage } from '@/types/domainKnowledge';

/** Project the saved reading outline to authored articles without changing it.
 * Source and table pages remain in the full catalog for explicit links.
 */
export function projectArticleReading(
  catalog: WikiReaderPage[], outline: WikiOutline | null, unlistedTitle: string,
): { pages: WikiReaderPage[]; outline: WikiOutline | null } {
  const pages = catalog.filter((page) => page.type === 'article');
  if (!outline) return { pages, outline: null };

  const allowed = new Set(pages.map((page) => page.path));
  const seen = new Set<string>();
  const project = (node: WikiOutlineNode): WikiOutlineNode | null => {
    const entries = (node.entries || []).filter((entry) => {
      if (!allowed.has(entry.path)) return false;
      seen.add(entry.path);
      return true;
    });
    const children = (node.children || []).map(project).filter((child): child is WikiOutlineNode => child !== null);
    return entries.length || children.length ? { ...node, entries, children } : null;
  };
  const topics = outline.topics.map(project).filter((topic): topic is WikiOutlineNode => topic !== null);
  const unlisted = pages.filter((page) => !seen.has(page.path));
  if (unlisted.length) {
    topics.push({
      id: '__authored_articles_unlisted__', kind: 'topic', title: unlistedTitle, description: '', order: topics.length,
      children: [], entries: unlisted.map((page, order) => ({
        path: page.path, title: page.title, role: 'article', category: page.category, note: '', order,
      })),
    });
  }
  return { pages, outline: { ...outline, topics, pageCount: pages.length, coveredCount: pages.length } };
}
