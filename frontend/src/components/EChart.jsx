import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart, BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, VisualMapComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent,
  MarkLineComponent, VisualMapComponent, CanvasRenderer]);

export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function baseOption() {
  const muted = cssVar("--muted"), line = cssVar("--line"), fg = cssVar("--fg");
  return {
    animation: false,
    textStyle: { color: fg },
    grid: { left: 8, right: 12, top: 28, bottom: 8, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: cssVar("--card"), borderColor: line, textStyle: { color: fg } },
    legend: { top: 0, textStyle: { color: muted }, itemWidth: 12, itemHeight: 8 },
    xAxis: { axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted }, splitLine: { show: false } },
    yAxis: { axisLabel: { color: muted }, splitLine: { lineStyle: { color: line } } },
  };
}

export default function EChart({ option, className = "chart", onEvents }) {
  const el = useRef(null);
  const chart = useRef(null);
  useEffect(() => {
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); chart.current = null; };
  }, []);
  useEffect(() => { if (option) chart.current?.setOption(option, { notMerge: true, lazyUpdate: true }); }, [option]);
  useEffect(() => {
    if (!onEvents) return;
    for (const [k, fn] of Object.entries(onEvents)) chart.current?.on(k, fn);
    return () => { for (const [k, fn] of Object.entries(onEvents)) chart.current?.off(k, fn); };
  }, [onEvents]);
  return <div ref={el} className={className} />;
}
