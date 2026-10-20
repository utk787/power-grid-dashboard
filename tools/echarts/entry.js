// Only the chart types and components the dashboard uses; everything else is tree-shaken out.
import * as echarts from 'echarts/core';
import { LineChart, BarChart, HeatmapChart, CustomChart } from 'echarts/charts';
import {
  GridComponent, TooltipComponent, DataZoomSliderComponent, VisualMapComponent,
  CalendarComponent, MarkAreaComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart, BarChart, HeatmapChart, CustomChart,
  GridComponent, TooltipComponent, DataZoomSliderComponent, VisualMapComponent,
  CalendarComponent, MarkAreaComponent,
  CanvasRenderer,
]);

export const { init, use, graphic, time, format, getInstanceByDom } = echarts;
