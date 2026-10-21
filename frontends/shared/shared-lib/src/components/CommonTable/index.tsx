import { forwardRef, useCallback, useImperativeHandle, useState, type ReactElement, type Ref } from 'react';
import { Button, Col, Empty, Form, Row, Space, Table } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import FilterField from './FilterField';
import { useTableQuery } from './useTableQuery';
import type { CommonTableProps, CommonTableRef } from './types';

const DEFAULT_FILTER_COLS = 4;
const GRID_COLUMNS = 24;
/** 筛选区栅格的竖直间距（Row 的 gutter）。 */
const FILTER_ITEM_GAP = 12;
/**
 * label 定宽、控件吃满其余空间。定宽是必须的：label 若按内容宽度自适应，
 * 「关键词」和「文档状态」差一个字，同一行里两个控件的宽度就对不齐了。
 * 5.5em 容得下 4 个汉字加冒号；更长的 label（英文）会折行，但不至于撑破栅格。
 */
const FILTER_LABEL_COL = { flex: '0 0 5.5em', style: { textAlign: 'right' as const } };
// basis 必须是 0 而不是 auto：auto 下控件按内容撑开，label + 控件超过行宽会被 flex 折行，
// label 就掉到控件上方去了。minWidth 0 是配套的——输入框是 width:100%，不设会被自身内容顶开。
const FILTER_WRAPPER_COL = { flex: '1 1 0%', style: { minWidth: 0 } };

function CommonTableInner<Row extends object, F extends Record<string, unknown>>(
  {
    filters = [],
    initialFilters,
    filterColsPerRow = DEFAULT_FILTER_COLS,
    filterExtra,
    filterExtraRow,
    toolbar,
    actions,
    columns,
    rowKey,
    emptyText,
    fetch,
    onError,
    pagination = 'server',
    defaultPageSize = 10,
    className,
    expandable,
    rowSelection,
    onRow,
    rowClassName,
    scroll,
    size,
    onQueryChange,
  }: CommonTableProps<Row, F>,
  ref: Ref<CommonTableRef>,
) {
  const { t } = useTranslation();
  const paginated = pagination === 'server';
  const [resetVersion, setResetVersion] = useState(0);

  const {
    list,
    total,
    loading,
    page,
    pageSize,
    setPage,
    setPageSize,
    refresh,
    reset,
    filterContext,
  } = useTableQuery<Row, F>({ fetch, paginated, defaultPageSize, initialFilters, onError, onQueryChange });

  const handleReset = useCallback(() => {
    reset();
    // 换 key 逼筛选项重新挂载：input 的防抖计时器和尚未落地的本地文本都随之清掉。
    // 少了这步，「刚敲完还没到防抖时间就点重置」会在 300ms 后把值又写回去。
    setResetVersion((v) => v + 1);
  }, [reset]);

  // reset 一并开出去：页面用 `actions.reset=false` 关掉内置按钮、自己摆放「重置」时，
  // 直接调它即可 —— 上面那段防抖收尾页面复刻不了，只能由组件自己做完。
  useImperativeHandle(ref, () => ({ refresh, reset: handleReset }), [refresh, handleReset]);

  // filterExtraRow 也是筛选控件（它拿到的是同一份上下文），只有它时同样要渲染筛选区与「重置」
  const hasFilters = filters.length > 0 || Boolean(filterExtra) || Boolean(filterExtraRow);
  const showRefresh = actions?.refresh !== false;
  // 没有筛选控件时「重置」无事可做，直接不渲染，免得留一个点了没反应的按钮
  const showReset = actions?.reset !== false && hasFilters;
  const hasButtons = showRefresh || showReset || Boolean(toolbar);
  const hasFilterBar = hasFilters || hasButtons;

  // 24 栅格按列数切分。非整除的取值（如 5）向下取整，宁可末列留白也不要挤破一行
  const colSpan = Math.floor(GRID_COLUMNS / Math.max(1, filterColsPerRow));

  return (
    <div className={className} style={{ display: 'flex', flexDirection: 'column', gap: 12, minHeight: 0 }}>
      {hasFilterBar && (
        // 阻止原生 submit：页面若在 toolbar 里放裸 <button> 会默认提交并刷新页面
        <Form layout="horizontal" onSubmitCapture={(e) => e.preventDefault()}>
          {/* 栅格纵向间距交给 Row 的 gutter：这样换行后的每一行、以及右侧按钮区，
              都吃到同一份间距，不必再让各 Col 各自抵消 Form.Item 的下边距 */}
          <Row gutter={[16, FILTER_ITEM_GAP]} align="middle">
            {filters.map((config) => (
              <Col key={`${config.key}-${resetVersion}`} span={colSpan}>
                <Form.Item
                  label={config.label}
                  labelCol={FILTER_LABEL_COL}
                  wrapperCol={FILTER_WRAPPER_COL}
                  style={{ marginBottom: 0 }}
                >
                  <FilterField config={config} ctx={filterContext} />
                </Form.Item>
              </Col>
            ))}
            {/* filterExtra 与配置项同处一个栅格；需要 label 的话页面自己包 <Form.Item> */}
            {filterExtra && (
              <Col key={`extra-${resetVersion}`} span={colSpan}>
                {filterExtra(filterContext)}
              </Col>
            )}
            {hasButtons && (
              <Col flex="auto" style={{ textAlign: 'right' }}>
                <Space>
                  {toolbar}
                  {showRefresh && (
                    <Button icon={<ReloadOutlined />} onClick={() => refresh()}>
                      {t('common.refresh')}
                    </Button>
                  )}
                  {showReset && <Button onClick={handleReset}>{t('common.reset')}</Button>}
                </Space>
              </Col>
            )}
          </Row>
          {/* 整行扩展位：在栅格之外，拿得到筛选上下文但不与筛选项争列宽 */}
          {filterExtraRow && (
            <div style={{ marginTop: FILTER_ITEM_GAP }}>{filterExtraRow(filterContext)}</div>
          )}
        </Form>
      )}

      {/* 失败时 hook 已清空 list，所以这里不必再判断 list.length ——
          取数失败与「本来就没数据」长得一样，报警由页面在 onError 里发（见 README「三态」） */}
      <Table<Row>
        rowKey={rowKey}
        columns={columns}
        dataSource={list}
        loading={loading}
        size={size}
        expandable={expandable}
        rowSelection={rowSelection}
        onRow={onRow}
        rowClassName={rowClassName}
        scroll={scroll}
        locale={{
          emptyText: emptyText ?? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />
          ),
        }}
        pagination={
          paginated
            ? {
                current: page,
                pageSize,
                total,
                showSizeChanger: true,
                // common.refresh / totalItems 等键定义在 @jonex/i18n-resources，各子应用
                // 经 createI18nInstance 合并后都能取到，不需要 defaultValue 兜底
                showTotal: (n) => t('common.totalItems', { total: n }),
                onChange: (nextPage, nextPageSize) => {
                  setPage(nextPage);
                  setPageSize(nextPageSize);
                },
              }
            : false
        }
      />
    </div>
  );
}

/**
 * forwardRef 无法直接承载泛型组件，所以先声明泛型内层函数，再对 forwardRef 的结果做一次
 * 类型断言把泛型补回签名上。这是 React 18 下的标准写法（React 19 起 ref 可直接作 prop）。
 */
const CommonTable = forwardRef(CommonTableInner) as <
  Row extends object,
  F extends Record<string, unknown>,
>(
  props: CommonTableProps<Row, F>,
) => ReactElement;

export default CommonTable;
