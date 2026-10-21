/**
 * ECharts 按需注册。
 *
 * 走 `echarts/core` 而不是 `import * as echarts from 'echarts'`：后者会把全部
 * 图表类型与组件打进产物，而这里只用得到一个 series 类型。新增图表种类时
 * 在这个文件里追加注册，不要在业务代码里 `import 'echarts'`。
 */
import * as echarts from 'echarts/core';
import { GraphChart } from 'echarts/charts';
import { LegendComponent, ThumbnailComponent, TooltipComponent } from 'echarts/components';
import { LabelLayout } from 'echarts/features';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  GraphChart,
  LegendComponent,
  TooltipComponent,
  ThumbnailComponent,
  LabelLayout,
  CanvasRenderer,
]);

export default echarts;
