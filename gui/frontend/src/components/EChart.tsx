import type { EChartsOption } from "echarts";
import { BarChart, CustomChart, GaugeChart, GraphChart, LineChart, TreeChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use, type EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

use([
  LineChart,
  BarChart,
  GaugeChart,
  TreeChart,
  GraphChart,
  CustomChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AriaComponent,
  CanvasRenderer,
]);

export interface EChartProps {
  option: EChartsOption;
  height?: number;
  ariaLabel: string;
  className?: string;
}

export function EChart({ option, height = 320, ariaLabel, className }: EChartProps): React.JSX.Element {
  const elementRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsType | undefined>(undefined);

  useEffect(() => {
    if (!elementRef.current) return;
    const chart = init(elementRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(elementRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = undefined;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return (
    <div
      ref={elementRef}
      className={className ? `chart ${className}` : "chart"}
      style={{ height }}
      role="img"
      aria-label={ariaLabel}
    />
  );
}

export const chartTheme = {
  ink: "#dce9e4",
  muted: "#80948e",
  grid: "rgba(130, 164, 152, 0.14)",
  blue: "#4fb4c2",
  gold: "#d8ad56",
  orange: "#d47b55",
  olive: "#88a36d",
  pink: "#c9879a",
  dark: "#0b1817",
  palette: ["#4fb4c2", "#d8ad56", "#d47b55", "#88a36d", "#c9879a"],
} as const;

export const baseGrid = {
  left: 52,
  right: 24,
  top: 42,
  bottom: 44,
  containLabel: true,
};

export const baseTooltip = {
  trigger: "axis" as const,
  renderMode: "richText" as const,
  backgroundColor: "#102320",
  borderColor: "rgba(139, 183, 168, .35)",
  textStyle: { color: chartTheme.ink, fontFamily: "IBM Plex Mono, ui-monospace, monospace" },
};

export const baseAxis = {
  axisLine: { lineStyle: { color: "rgba(139, 183, 168, .28)" } },
  axisTick: { show: false },
  axisLabel: { color: chartTheme.muted, fontFamily: "IBM Plex Mono, ui-monospace, monospace" },
  splitLine: { lineStyle: { color: chartTheme.grid } },
};
