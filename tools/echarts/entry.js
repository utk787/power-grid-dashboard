// Only the chart types and components the dashboard uses; everything else is tree-shaken out.
import * as echarts from 'echarts/core';
import { LineChart, BarChart, HeatmapChart, CustomChart } from 'echarts/charts';
import {
  GridComponent, TooltipComponent, DataZoomSliderComponent, VisualMapComponent,
  CalendarComponent, MarkAreaComponent, ToolboxComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart, BarChart, HeatmapChart, CustomChart,
  GridComponent, TooltipComponent, DataZoomSliderComponent, VisualMapComponent,
  CalendarComponent, MarkAreaComponent, ToolboxComponent,  // toolbox: drag-to-select zoom on "Power on / off"
  CanvasRenderer,
]);

export const { init, use, graphic, time, format, getInstanceByDom } = echarts;
