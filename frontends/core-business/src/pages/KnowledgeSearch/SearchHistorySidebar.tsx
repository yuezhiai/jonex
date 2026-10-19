import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Spin, Typography } from 'antd';
import { HistoryOutlined } from '@ant-design/icons';
import classNames from 'classnames';
import { getQueryImageViewTicket } from '@/api/knowledgeSearch';
import type { KnowledgeSearchHistoryItem } from '@/types/knowledgeSearch';
import styles from './SearchHistorySidebar.module.scss';

function formatRelativeTime(isoStr: string, t: (key: string, opts?: any) => string): string {
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return t('common.justNow');
  if (diffMin < 60) return t('common.minutesAgo', { count: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t('common.hoursAgo', { count: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay === 1) return t('common.yesterday');
  if (diffDay < 7) return t('common.daysAgo', { count: diffDay });
  return isoStr.substring(0, 10);
}


interface SearchHistorySidebarProps {
  history: KnowledgeSearchHistoryItem[];
  activeHistoryIndex: number | null;
  onHistoryClick: (item: KnowledgeSearchHistoryItem, index: number) => void;
  onDeleteHistory: (id: string, index: number) => void;
  onClearHistory: () => void;
  // [jonex] 历史分页："查看更多"按钮（已加载条数 < 总数时展示）
  hasMore?: boolean;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  /**
   * [jonex] 正在按 id 拉快照的那条历史的 id。
   * 列表不再携带快照（answer / references / reasoning 占响应体积 83%），
   * 点击时才取单条 —— 期间在这一条上转圈，而不是让整页等。
   */
  loadingId?: string | null;
}

export default function SearchHistorySidebar({
  history,
  activeHistoryIndex,
  onHistoryClick,
  onDeleteHistory,
  onClearHistory,
  hasMore,
  onLoadMore,
  loadingMore,
  loadingId,
}: SearchHistorySidebarProps) {
  const { t } = useTranslation();

  // [jonex] 以图问知识：列表里的 view_url 是后端补签的短时票据（默认 15 分钟），
  // 侧栏常驻页面时容易过期。加载失败就按 image_id 重签一次并缓存。
  // ⚠️ 用 ref 记账已重试过的 id：在 setState 的更新函数里发请求是反模式
  // （StrictMode 下更新函数跑两次 = 两次请求），而且 onError 本身会随重渲染反复触发。
  const [ticketMap, setTicketMap] = React.useState<Record<string, string>>({});
  const retriedRef = React.useRef<Set<string>>(new Set());
  // 重签过一次仍然失败 → 判定为真的取不到（已清理 / 后端异常），
  // 渲染占位块而不是让浏览器显示**破图图标**（那是"页面坏了"的观感）
  const [failedIds, setFailedIds] = React.useState<Set<string>>(() => new Set());
  const markFailed = React.useCallback((imageId: string) => {
    setFailedIds((prev) => (prev.has(imageId) ? prev : new Set(prev).add(imageId)));
  }, []);
  const refreshThumb = React.useCallback(
    (imageId: string) => {
      if (retriedRef.current.has(imageId)) {
        markFailed(imageId);
        return;
      }
      retriedRef.current.add(imageId);
      void getQueryImageViewTicket(imageId)
        .then((ticket) => setTicketMap((prev) => ({ ...prev, [imageId]: ticket.url })))
        .catch(() => markFailed(imageId));
    },
    [markFailed],
  );

  return (
    <div className={styles.sidebar}>
      <div className={styles.header}>
        <span className={styles.headerIcon}>
          <HistoryOutlined />
        </span>
        <span className={styles.title}>{t('knowledgeSearch.searchHistory')}</span>
        {history.length > 0 && <span className={styles.count}>{history.length}</span>}
        {history.length > 0 && (
          <Button type="text" size="small" className={styles.clearBtn} onClick={onClearHistory}>
            {t('knowledgeSearch.clear')}
          </Button>
        )}
      </div>
      {history.length === 0 ? (
        <div className={styles.empty}>
          <span className={styles.emptyIcon}>
            <HistoryOutlined />
          </span>
          <span className={styles.emptyText}>{t('knowledgeSearch.noSearchHistory')}</span>
        </div>
      ) : (
        <div className={styles.list}>
          {history.map((h, i) => (
            <div
              key={h.id}
              className={classNames(styles.item, { [styles.itemActive]: activeHistoryIndex === i })}
            >
              {/* 点击区是 antd Button；删除按钮作为它的兄弟节点，避免按钮嵌套 */}
              <Button
                type="text"
                className={styles.itemMain}
                onClick={() => onHistoryClick(h, i)}
                aria-current={activeHistoryIndex === i ? 'true' : undefined}
              >
                {/* [jonex] 以图问知识：有图的条目左侧显示第一张缩略图，多张叠加 +N 角标 */}
                {Boolean(h.queryImages?.length) && (
                  <div className={styles.thumb}>
                    {(ticketMap[h.queryImages![0].id] || h.queryImages![0].viewUrl) &&
                    !failedIds.has(h.queryImages![0].id) ? (
                      <img
                        className={styles.thumbImg}
                        src={(ticketMap[h.queryImages![0].id] || h.queryImages![0].viewUrl) ?? undefined}
                        alt={t('knowledgeSearch.uploadImage')}
                        loading="lazy"
                        onError={() => refreshThumb(h.queryImages![0].id)}
                      />
                    ) : (
                      // 票据没签出来 / 已清理：给个占位，条目其余内容照常
                      <div className={styles.thumbPlaceholder} />
                    )}
                    {h.queryImages!.length > 1 && (
                      <span className={styles.thumbBadge}>+{h.queryImages!.length - 1}</span>
                    )}
                  </div>
                )}
                <div className={styles.body}>
                  {/* 有原话显示原话（用户认得出），否则显示实际用于检索的文本；
                      单行省略与悬停查看全文交给 Typography 自带能力 */}
                  <Typography.Text
                    style={{
                      display: 'block',
                      fontSize: 13,
                      fontWeight: 500,
                      lineHeight: 1.45,
                      color: '#1e293b',
                      marginBottom: 4,
                    }}
                    ellipsis={{ tooltip: h.imageQuestion || h.query }}
                  >
                    {h.imageQuestion || h.query}
                  </Typography.Text>
                  <div className={styles.meta}>
                    <span>{t('knowledgeSearch.resultsCount', { count: h.resultCount })}</span>
                    <span className={styles.metaDot} />
                    <span>{formatRelativeTime(h.searchedAt, t)}</span>
                  </div>
                </div>
              </Button>
              {/* [jonex] 拉快照期间占位转圈，避免用户以为点击没生效 */}
              {loadingId === h.id ? (
                <Spin size="small" className={styles.spinner} />
              ) : (
                <Button
                  type="text"
                  size="small"
                  className={styles.deleteBtn}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteHistory(h.id, i);
                  }}
                  title={t('knowledgeSearch.deleteHistory')}
                >
                  ×
                </Button>
              )}
            </div>
          ))}
          {hasMore && (
            <div className={styles.loadMore}>
              <Button type="link" size="small" loading={loadingMore} onClick={onLoadMore}>
                {t('knowledgeSearch.loadMore')}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
