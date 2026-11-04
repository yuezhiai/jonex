import { useState, useEffect, useCallback, useRef } from 'react';
import { Button, Card, Empty, Input, Pagination, Result, Segmented, Skeleton, Tabs, Tooltip, message } from 'antd';
import { InfoCircleOutlined, SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { colors } from '@jonex/platform-theme/tokens';
import { PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import { fetchSkills, enableSkill, disableSkill, type SkillItem, type FetchSkillsParams } from '../../api/skills';
import { CATEGORY_LABEL_KEYS } from './shared';
import SkillCard from './SkillCard';
import SkillDetailModal from './SkillDetailModal';
import CatalogAdmin from './CatalogAdmin';
import './index.css';

const CATEGORY_OPTIONS = ['image', 'voice', 'document', 'video', 'fusion', 'custom'] as const;
const PAGE_SIZE = 12;
const SEARCH_DEBOUNCE_MS = 300;

export default function Skills() {
  const { t } = useTranslation();
  const [items, setItems] = useState<SkillItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const [category, setCategory] = useState<string | undefined>();
  const [page, setPage] = useState(1);
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [detailSkill, setDetailSkill] = useState<SkillItem | null>(null);
  /** 请求序号：快速切换筛选时丢弃过期响应，避免旧结果覆盖新结果 */
  const requestSeq = useRef(0);
  // 工具目录管理只给平台管理员（目录对所有租户生效，管理视图含运行时地址）；后端同样校验
  const { hasPerm } = usePermission();
  const isPlatformAdmin = hasPerm('platform:admin');

  const loadSkills = useCallback(
    async (params: FetchSkillsParams, silent = false) => {
      const seq = ++requestSeq.current;
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      try {
        const result = await fetchSkills(params);
        if (seq !== requestSeq.current) return;
        setItems(result.items);
        setTotal(result.total);
      } catch (err: unknown) {
        if (seq !== requestSeq.current) return;
        // 静默刷新失败不打断目录管理页签，保留当前列表
        if (!silent) setError(err instanceof Error ? err.message : t('skills.loadFailed'));
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [t],
  );

  // 分页 / 关键词 / 分类都由后端过滤：前端只维护查询参数，不做当前页二次过滤
  useEffect(() => {
    loadSkills({
      offset: (page - 1) * PAGE_SIZE,
      limit: PAGE_SIZE,
      keyword: debouncedKeyword || undefined,
      category,
    });
  }, [page, debouncedKeyword, category, loadSkills]);

  // 输入防抖：停止输入 300ms 后才重置到第一页并重新请求
  useEffect(() => {
    const next = keyword.trim();
    if (next === debouncedKeyword) return;
    const timer = setTimeout(() => {
      setDebouncedKeyword(next);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [keyword, debouncedKeyword]);

  const currentParams = useCallback(
    (): FetchSkillsParams => ({
      offset: (page - 1) * PAGE_SIZE,
      limit: PAGE_SIZE,
      keyword: debouncedKeyword || undefined,
      category,
    }),
    [page, debouncedKeyword, category],
  );

  async function handleToggle(skill: SkillItem) {
    setSubmittingId(skill.id);
    try {
      if (skill.enabled) {
        await disableSkill(skill.id);
        message.success(t('skills.disabledSkill'));
      } else {
        await enableSkill(skill.id);
        message.success(t('skills.enabledSkill'));
      }
      // 只更新本条，避免整页切回加载态、丢失滚动位置
      setItems((prev) => prev.map((item) => (item.id === skill.id ? { ...item, enabled: !skill.enabled } : item)));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.operationFailed'));
    } finally {
      setSubmittingId(null);
    }
  }

  const reload = () => loadSkills(currentParams());

  // 目录管理页签改动后静默刷新租户视图：不走 loading 态，避免整页切到骨架把管理页签卸载
  const refreshSilently = () => loadSkills(currentParams(), true);

  const hasFilters = !!debouncedKeyword || !!category;
  const clearFilters = () => {
    setKeyword('');
    setDebouncedKeyword('');
    setCategory(undefined);
    setPage(1);
  };

  const categoryOptions = [
    { label: t('skills.categoryAll'), value: '' },
    ...CATEGORY_OPTIONS.map((c) => ({ label: t(CATEGORY_LABEL_KEYS[c]), value: c })),
  ];

  const tenantView = (
    <>
      <div className="skills-filter">
        <Input
          placeholder={t('skills.searchPlaceholder')}
          prefix={<SearchOutlined />}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          allowClear
          style={{ width: 280 }}
        />
        <div className="skills-category-scroll">
          <Segmented
            value={category ?? ''}
            onChange={(value) => {
              setCategory(value === '' ? undefined : String(value));
              setPage(1);
            }}
            options={categoryOptions}
          />
        </div>
        {/* 加载中 total 还是上一轮的值（首屏为 0），先不显示避免「共 0 个工具」误导 */}
        {!loading && <span className="skills-count">{t('skills.countText', { count: total })}</span>}
      </div>

      {loading ? (
        <div className="skills-grid">
          {Array.from({ length: 6 }).map((_, index) => (
            <Card key={index} className="skill-card">
              <Skeleton active title paragraph={{ rows: 2 }} />
            </Card>
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="skills-empty">
          <Empty description={t('skills.emptyText')}>
            {hasFilters ? (
              <Button onClick={clearFilters}>{t('common.clearFilters')}</Button>
            ) : (
              <Button onClick={reload}>{t('common.refresh')}</Button>
            )}
          </Empty>
        </div>
      ) : (
        <>
          <div className="skills-grid">
            {items.map((skill) => (
              <SkillCard
                key={skill.id}
                skill={skill}
                busy={submittingId === skill.id}
                onToggle={handleToggle}
                onOpen={setDetailSkill}
                t={t}
              />
            ))}
          </div>
          {total > PAGE_SIZE && (
            <div className="skills-pagination">
              <Pagination
                current={page}
                pageSize={PAGE_SIZE}
                total={total}
                showSizeChanger={false}
                onChange={setPage}
              />
            </div>
          )}
        </>
      )}
    </>
  );

  return (
    <>
      <PageHeader title={t('skills.pageTitle')} style={{ marginBottom: 4 }} />
      <div className="skills-subtitle">
        <span className="skills-subtitle-text">{t('skills.pageSubtitle')}</span>
        <Tooltip title={t('skills.pageSubtitle')}>
          <InfoCircleOutlined className="skills-subtitle-info" style={{ color: colors.textMuted }} />
        </Tooltip>
      </div>

      {/* 卡片自身去内边距，页签栏贴顶，留白交给内容区统一给 */}
      <PageContent height="content" className="skills-panel" styles={{ body: { padding: 0 } }}>
        {error ? (
          <div className="skills-panel-plain">
            <Result
              status="error"
              title={t('common.loadFailed')}
              subTitle={error}
              extra={
                <Button type="primary" onClick={reload}>
                  {t('common.retry')}
                </Button>
              }
            />
          </div>
        ) : isPlatformAdmin ? (
          <Tabs
            defaultActiveKey="tenant"
            items={[
              { key: 'tenant', label: t('skills.catalog.tabTenant'), children: tenantView },
              {
                key: 'catalog',
                label: t('skills.catalog.tabCatalog'),
                children: <CatalogAdmin onChanged={refreshSilently} />,
              },
            ]}
          />
        ) : (
          <div className="skills-panel-plain">{tenantView}</div>
        )}
      </PageContent>

      <SkillDetailModal open={!!detailSkill} skill={detailSkill} onClose={() => setDetailSkill(null)} />
    </>
  );
}
