/** W4.6 结果契约的块类型（后端 `result_contract` 的 `blocks`）。 */
export interface ResultTextBlock {
  type: 'text';
  text: string;
}

export interface ResultColumn {
  name: string;
  type?: string;
}

export interface ResultTableBlock {
  type: 'table';
  columns: ResultColumn[];
  rows: unknown[][];
  row_count?: number;
  truncated?: boolean;
}

export interface ResultChartSeries {
  name: string;
  field: string;
}

export interface ResultChartBlock {
  type: 'chart';
  chart_type: 'line' | 'bar';
  x: string;
  series: ResultChartSeries[];
}

export type ResultBlock = ResultTextBlock | ResultTableBlock | ResultChartBlock;
