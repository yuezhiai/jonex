import type { Dayjs } from 'dayjs';

/**
 * 任务中心的页面层类型。
 *
 * 接口 DTO 不在这里 —— 它们在 `src/api/taskCenter.ts`，与请求函数共存（同 auditLogs.ts
 * 等模块的惯例）。本文件只剩页面自己的东西：筛选值。
 */

/**
 * 筛选值。key 与 CommonTable 的 filters[].key 一一对应，也是 fetch 参数的字段名。
 *
 * 必须是 type 别名而非 interface：CommonTable 的 `F extends Record<string, unknown>`
 * 要求隐式索引签名，TS 只给类型别名补、不给 interface 补（TS #15300）。
 */
export type TaskFilters = {
  keyword?: string;
  /**
   * 日期区间 [起始, 结束]；fetch 里拆成 start_time / end_time。
   *
   * 存 dayjs 对象而不是 ISO 串：RangePicker 的 value 要 dayjs，存串就得在 value 上反解析一次。
   * 与同子应用 OperationLog 的做法一致。
   */
  range?: [Dayjs, Dayjs] | null;
  /** 由状态标签栏经 filterExtraRow 写入 —— 它不在 filters 数组里，但同属这份筛选值。 */
  status?: string;
  /**
   * 任务来源码（`knowledge_base.file.*`）。不选为全部。
   *
   * ⚠️ 空值那种情况**筛不到** —— 后端是「有值才加条件」，没有「平台自建」这一档。
   */
  task_source?: string;
};
