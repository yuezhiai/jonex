import type { HTMLAttributes, ReactNode } from 'react';
import type { TableColumnsType, TableProps } from 'antd';

/**
 * 筛选控件声明。key 同时是传给 fetch 的查询参数名，label 是表单左侧/上方的字段名。
 * label 传已翻译好的文本，组件不替页面做 i18n（与 placeholder 一致）。
 */
export type FilterConfig =
  | {
      key: string;
      label: string;
      type: 'input';
      placeholder?: string;
      /** 输入防抖毫秒数，默认 300；传 0 表示即时生效。 */
      debounce?: number;
      /** 覆盖默认的 100% 宽度，一般不必传——栅格已经定好每项的宽度。 */
      width?: number | string;
    }
  | {
      key: string;
      label: string;
      type: 'select';
      placeholder?: string;
      width?: number | string;
      mode?: 'multiple';
      /** 多选时最多平铺几个标签，超出的折叠成「+N」。 */
      maxTagCount?: number;
      options: { label: string; value: string }[];
    };

/** 暴露给自定义筛选控件（filterExtra）的读写句柄。 */
export interface FilterContext<F> {
  /** 当前已生效的筛选值，不含正在输入、尚未防抖落地的内容。 */
  values: Partial<F>;
  get: <K extends keyof F>(key: K) => F[K] | undefined;
  /** 写入筛选值并自动重置 page = 1 —— 与配置项走同一条路径。 */
  set: <K extends keyof F>(key: K, value: F[K]) => void;
}

export interface CommonTableRef {
  /**
   * 用当前筛选条件与页码重新拉取。resetPage 为 true 时先回到第 1 页。
   *
   * resetPage 是给「页面自己管理的查询条件」用的——比如 DocumentLibrary 左侧目录树选中的文件夹。
   * 这类条件进不了 filters（不是控件），CommonTable 看不见它变了；而页面本来就停在第 1 页时，
   * 光 setPage(1) 不产生状态变化，也就不会重取。所以这里始终靠 tick 兜底触发一次取数。
   *
   * silent 只改「怎么呈现」，不改「取什么」：不置 loading（表格不罩遮罩、不闪），
   * 失败也不清空列表。给轮询这类「用户没要求、但数据得跟上」的重取用；用户主动的
   * 刷新别用它——点了按钮却什么都没发生，会比闪一下更让人不安。
   */
  refresh: (options?: { resetPage?: boolean; silent?: boolean }) => void;
  /**
   * 清空全部筛选条件并回到第 1 页 —— 与筛选区那枚内置「重置」按钮是**同一个动作**
   * （含清掉输入框尚未落地的防抖值那步）。
   *
   * 给 `actions.reset=false` 关掉内置按钮、改由页面自己摆放按钮的场景用：页面能自己渲染
   * 「刷新」，却复刻不了重置的防抖收尾，所以把动作开出来而不是让页面用 `ctx.set` 逐 key 清。
   */
  reset: () => void;
}

/** 筛选区右侧按钮区的开关。两个按钮默认都显示，传 false 关掉。 */
export interface CommonTableActions {
  /** 用当前筛选条件与页码重新取数。默认 true。 */
  refresh?: boolean;
  /** 清空全部筛选条件并回到第 1 页。默认 true；页面没有任何筛选控件时不渲染。 */
  reset?: boolean;
}

/** fetch 收到的参数形状，不随分页模式变化。 */
export type TableQueryParams<F> = { page: number; pageSize: number } & Partial<F>;

export interface CommonTableProps<Row, F extends Record<string, unknown>> {
  filters?: FilterConfig[];
  /**
   * 首次挂载时的筛选初值（如从别的页面带过来的状态 / 时间区间）。
   *
   * **只在挂载时读一次**：之后由用户在本组件里的操作决定筛选值。做成受控 prop 的话，
   * 父组件每次渲染都可能把用户已经改过的筛选回写覆盖掉。想换初值只能换 `key` 重挂载。
   */
  initialFilters?: Partial<F>;
  /** 筛选区每行放几个筛选项，默认 4。按 24 栅格整除，建议取 2 / 3 / 4 / 6。 */
  filterColsPerRow?: number;
  /**
   * 配置项表达不了的筛选控件（如日期区间）。render prop 而非普通插槽，见规格 §3.1。
   * 「一个 key + 一个 antd 控件」的场景一律走 filters，不要用这里。
   */
  filterExtra?: (ctx: FilterContext<F>) => ReactNode;
  /**
   * 筛选行之下、表格之上的整行扩展位，独占一行（不参与上方栅格的分栏）。
   *
   * 与 `filterExtra` 的区别是位置与用途：`filterExtra` 在栅格内占一格，放单个配置项
   * 表达不了的控件（日期区间等）；这里放需要整行宽度、且**参与筛选**的控件组（如状态标签栏）。
   * 两者都拿得到 FilterContext，写入的值与配置项走同一条路径，「重置」时一并清掉。
   *
   * 纯操作按钮请用 `toolbar` —— 它在筛选区右侧，且拿不到 FilterContext。
   */
  filterExtraRow?: (ctx: FilterContext<F>) => ReactNode;
  /** 额外的操作按钮（新增 / 批量导出等），与默认按钮同处筛选区右侧。 */
  toolbar?: ReactNode;
  /** 刷新 / 重置两个默认按钮的显示开关。 */
  actions?: CommonTableActions;

  columns: TableColumnsType<Row>;
  rowKey: string | ((row: Row) => string);
  emptyText?: ReactNode;

  /**
   * 取数函数。始终收到 { page, pageSize } 合并筛选值的单一对象。
   * pagination 为 false 时只会被调用一次，page 恒为 1，忽略这两个字段即可。
   */
  fetch: (params: TableQueryParams<F>) => Promise<{ list: Row[]; total: number }>;
  /**
   * 取数失败。`silent` 为 true 表示这次失败来自 `refresh({ silent: true })`（轮询）——
   * 组件已经按静默处理了（保留列表、不动 loading），页面通常只该记一笔、别弹提示，
   * 否则轮询会把同一个错误按间隔反复弹给用户。
   */
  onError?: (e: unknown, info: { silent: boolean }) => void;
  ref?: React.Ref<CommonTableRef>;

  /** 'server'：服务端分页；false：不分页（只拉一次）。 */
  pagination?: 'server' | false;
  defaultPageSize?: number;

  // ── 表格自身的额外能力 ──
  // 下面这些是组件无法替页面决定的：组件只保证它们是「具名的、可枚举的」，
  // 不开 tableProps 之类的通用透传口子，免得 API 退化成「把所有页面逻辑重新排列一遍」。

  /** 根容器的 class。页面用它承接宿主高度 / flex 布局（列表页常需要撑满并在内部滚动）。 */
  className?: string;
  /** 行展开（如父任务行下渲染子阶段）。 */
  expandable?: TableProps<Row>['expandable'];
  /** 行选择（批量操作）。 */
  rowSelection?: TableProps<Row>['rowSelection'];
  /** 行级 DOM 事件，如 HTML5 拖拽（draggable / onDragStart / onDragEnd）。 */
  onRow?: (row: Row, index?: number) => HTMLAttributes<HTMLElement>;
  /** 行 class，如拖拽中高亮。 */
  rowClassName?: (row: Row, index: number) => string;
  /** 固定表头 / 横向滚动。y 只吃具体像素，页面自己量宿主容器高度。 */
  scroll?: TableProps<Row>['scroll'];
  /** antd Table 默认 large，列表页一般传 middle。 */
  size?: TableProps<Row>['size'];

  /**
   * 查询条件（页码 / 每页条数 / 筛选值）变化时回调，首次取数也会触发一次。
   * ref.refresh() 只是重取同一份数据、条件没变，所以不触发。
   * 页面用它清空行选中——翻页或改筛选后，旧选中可能已不在当前结果集里，批量操作会打错对象。
   */
  onQueryChange?: (params: TableQueryParams<F>) => void;
}
