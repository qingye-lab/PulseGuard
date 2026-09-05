import type { G2Spec, LineMark, ViewComposition } from "@antv/g2";
import { Alert } from "antd";
import dayjs from "dayjs";
import { useEffect, useMemo, useRef, useState } from "react";
import { palette } from "../designSystem";
import type { TrendPoint } from "../types";
import { formatDuration } from "../utils";
import { LatencyRuntime } from "./latencyChartRuntime";

const CHART_COLORS = {
  avg: palette.primary,
  p95: palette.warning,
  p99: palette.danger,
  grid: palette.border
} as const;

// Only the latency chart's line/point configuration is adapted here. Rendering,
// axes, legend, tooltip, auto-fit and teardown remain owned by the AntV runtime.
type LatencyPlotOptions = Omit<ViewComposition, "children" | "data" | "style" | "tooltip"> & {
  data: ChartDatum[];
  xField: string;
  yField: string;
  colorField: string;
  shapeField: "smooth";
  point?: boolean;
  style?: LineMark["style"];
  tooltip?: LineMark["tooltip"];
  autoFit?: boolean;
};

function PlotLine(props: LatencyPlotOptions) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<{
    instance: InstanceType<typeof LatencyRuntime>;
    pending: Promise<unknown> | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const options = useMemo<G2Spec>(() => {
    const { data, xField, yField, colorField, shapeField, point, style, ...view } = props;
    const axes = view.axis && typeof view.axis === "object" ? view.axis : {};
    return {
      ...view,
      type: "view",
      data,
      scale: { ...view.scale, shape: { type: "identity" }, size: { type: "identity" } },
      axis: view.axis === false ? false : {
        x: { title: false, labelOpacity: 1, ...(typeof axes.x === "object" ? axes.x : {}) },
        y: { title: false, labelOpacity: 1, ...(typeof axes.y === "object" ? axes.y : {}) }
      },
      legend: view.legend === false ? false : {
        ...(typeof view.legend === "object" ? view.legend : {}), shape: false, size: false
      },
      interaction: { tooltip: { shared: true } },
      children: [
        {
          type: "line",
          encode: { x: xField, y: yField, color: colorField, shape: { type: "constant", value: shapeField } },
          style,
          tooltip: view.tooltip
        },
        ...(point ? [{
          type: "point" as const,
          encode: {
            x: xField, y: yField, color: colorField,
            shape: { type: "constant", value: "circle" }, size: { type: "constant", value: 3 }
          },
          tooltip: false as const
        }] : [])
      ]
    } as G2Spec;
  }, [props]);

  useEffect(() => {
    if (!container.current) return;
    const instance = new LatencyRuntime({ container: container.current, autoFit: true });
    const session = { instance, pending: null as Promise<unknown> | null };
    chart.current = session;
    return () => {
      chart.current = null;
      // G2 queues overlapping render() calls using the latest options. Destroy
      // only after its final render settles: destroying an active canvas can
      // strand that promise (notably during React StrictMode's effect replay).
      if (session.pending) void session.pending.then(() => instance.destroy());
      else instance.destroy();
    };
  }, []);

  useEffect(() => {
    const session = chart.current;
    if (!session) return;
    let cancelled = false;
    setError(null);
    session.instance.options(options);
    session.pending = session.instance.render().catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : "趋势图加载失败");
    });
    return () => { cancelled = true; };
  }, [options]);

  return <>
    {error && <Alert type="error" title="趋势图加载失败" description={error} showIcon role="alert" />}
    <div ref={container} style={{ height: props.height }} aria-label="平均、P95、P99 响应时间趋势" />
  </>;
}

type ChartVariant = "summary" | "sparkline" | "drawer";

interface ChartDatum {
  bucket: number;
  metric: "平均" | "P95" | "P99";
  value: number;
}

type TickBucket = "minute" | "hourFine" | "hour" | "dayHour" | "day" | "week" | "month";

interface TickPolicy {
  tickCount: number;
  labelStep: number;
  labelFormatter: (value: number | Date) => string;
  bucket: TickBucket;
}

interface ChartYAxisPolicy {
  max: number;
  ticks: number[];
}

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const Y_AXIS_BASE_STEP = 100;

function pickStep(variant: ChartVariant, steps: { task: number; summary: number; drawer: number }): number {
  if (variant === "drawer") return steps.drawer;
  if (variant === "summary") return steps.summary;
  return steps.task;
}

function formatDayBoundary(value: number | Date, withTime: boolean): string {
  const m = dayjs(value);
  if (m.hour() === 0 && m.minute() === 0) return m.format("MM-DD");
  return withTime ? m.format("MM-DD HH:mm") : m.format("HH:mm");
}

function tickPolicy(spanMs: number, variant: ChartVariant): TickPolicy {
  if (spanMs <= 0 || !Number.isFinite(spanMs)) {
    return {
      tickCount: 12,
      labelStep: pickStep(variant, { task: 2, summary: 4, drawer: 2 }),
      labelFormatter: (value) => dayjs(value).format("HH:mm"),
      bucket: "hour"
    };
  }

  if (spanMs <= 2 * HOUR_MS) {
    return {
      tickCount: 12,
      labelStep: pickStep(variant, { task: 2, summary: 3, drawer: 2 }),
      labelFormatter: (value) => dayjs(value).format("HH:mm"),
      bucket: "minute"
    };
  }

  if (spanMs <= 12 * HOUR_MS) {
    return {
      tickCount: 12,
      labelStep: pickStep(variant, { task: 2, summary: 3, drawer: 2 }),
      labelFormatter: (value) => dayjs(value).format("HH:mm"),
      bucket: "hourFine"
    };
  }

  if (spanMs <= 36 * HOUR_MS) {
    return {
      tickCount: 12,
      labelStep: pickStep(variant, { task: 2, summary: 4, drawer: 2 }),
      labelFormatter: (value) => formatDayBoundary(value, false),
      bucket: "hour"
    };
  }

  if (spanMs <= 8 * DAY_MS) {
    return {
      tickCount: 14,
      labelStep: pickStep(variant, { task: 2, summary: 4, drawer: 2 }),
      labelFormatter: (value) => formatDayBoundary(value, true),
      bucket: "dayHour"
    };
  }

  if (spanMs <= 35 * DAY_MS) {
    return {
      tickCount: 15,
      labelStep: pickStep(variant, { task: 2, summary: 3, drawer: 2 }),
      labelFormatter: (value) => dayjs(value).format("MM-DD"),
      bucket: "day"
    };
  }

  if (spanMs <= 13 * 30 * DAY_MS) {
    return {
      tickCount: 13,
      labelStep: pickStep(variant, { task: 2, summary: 3, drawer: 2 }),
      labelFormatter: (value) => dayjs(value).format("MM-DD"),
      bucket: "week"
    };
  }

  return {
    tickCount: 12,
    labelStep: pickStep(variant, { task: 2, summary: 3, drawer: 2 }),
    labelFormatter: (value) => dayjs(value).format("YYYY-MM"),
    bucket: "month"
  };
}

function buildLabelFilter(step: number) {
  return (_item: unknown, index: number, all: unknown[]): boolean => {
    if (!all || all.length <= 1) return true;
    if (index === 0 || index === all.length - 1) return true;
    const safeStep = Math.max(1, Math.floor(step));
    return index % safeStep === 0;
  };
}

function pointsToData(points: TrendPoint[]): ChartDatum[] {
  const data: ChartDatum[] = [];
  points.forEach((point) => {
    const ts = dayjs(point.bucket_start).valueOf();
    if (!Number.isFinite(ts)) return;
    if (typeof point.avg_duration_ms === "number") {
      data.push({ bucket: ts, metric: "平均", value: point.avg_duration_ms });
    }
    if (typeof point.p95_duration_ms === "number") {
      data.push({ bucket: ts, metric: "P95", value: point.p95_duration_ms });
    }
    if (typeof point.p99_duration_ms === "number") {
      data.push({ bucket: ts, metric: "P99", value: point.p99_duration_ms });
    }
  });
  return data.sort((a, b) => a.bucket - b.bucket);
}

function buildCardYAxisPolicy(values: number[], variant: Extract<ChartVariant, "summary" | "sparkline">): ChartYAxisPolicy {
  const rawMax = Math.max(0, ...values);
  const roundedMax = Math.max(Y_AXIS_BASE_STEP, Math.ceil(rawMax / Y_AXIS_BASE_STEP) * Y_AXIS_BASE_STEP);
  const max = roundedMax + Y_AXIS_BASE_STEP;
  const targetSegments = variant === "summary" ? 5 : 4;
  const step = Math.max(
    Y_AXIS_BASE_STEP,
    Math.round(max / targetSegments / Y_AXIS_BASE_STEP) * Y_AXIS_BASE_STEP
  );
  const ticks: number[] = [];
  for (let value = 0; value < max; value += step) {
    ticks.push(value);
  }
  if (ticks[ticks.length - 1] !== max) {
    ticks.push(max);
  }
  return { max, ticks };
}

export default function LatencyChart({ points, variant }: { points: TrendPoint[]; variant: ChartVariant }) {
  const data = useMemo(() => pointsToData(points), [points]);
  const heightMap: Record<ChartVariant, number> = { summary: 190, sparkline: 168, drawer: 320 };
  const height = heightMap[variant];
  const isCardChart = variant !== "drawer";
  const isSummaryCard = variant === "summary";

  if (!data.length) {
    return (
      <div className="latency-chart latency-chart-empty" style={{ height }}>
        暂无成功响应时间
      </div>
    );
  }

  if (variant === "drawer") {
    const drawerSpan = data.length >= 2 ? data[data.length - 1].bucket - data[0].bucket : 0;
    const drawerPolicy = tickPolicy(drawerSpan, "drawer");
    return (
      <div className="latency-chart latency-chart-drawer">
        <PlotLine
          data={data}
          xField="bucket"
          yField="value"
          colorField="metric"
          shapeField="smooth"
          style={{
            lineWidth: (datum: ChartDatum) => (datum.metric === "平均" ? 2.2 : 1.5),
            lineDash: (datum: ChartDatum) => (datum.metric === "P99" ? [4, 3] : undefined)
          }}
          point
          scale={{
            x: { type: "time", nice: true },
            y: { nice: true },
            color: { domain: ["平均", "P95", "P99"], range: [CHART_COLORS.avg, CHART_COLORS.p95, CHART_COLORS.p99] }
          }}
          axis={{
            x: {
              tickCount: drawerPolicy.tickCount,
              labelFormatter: drawerPolicy.labelFormatter,
              labelFilter: buildLabelFilter(drawerPolicy.labelStep),
              labelFill: palette.textTertiary,
              labelAutoRotate: false,
              labelAutoHide: true,
              line: true,
              lineStroke: palette.border,
              tick: false
            },
            y: {
              labelFormatter: (value: number) => formatDuration(value),
              labelFill: palette.textTertiary,
              nice: true,
              grid: true,
              gridStroke: CHART_COLORS.grid,
              gridLineDash: [3, 3],
              line: false,
              tick: false
            }
          }}
          tooltip={{
            title: (datum: { bucket: number }) => dayjs(datum.bucket).format("YYYY-MM-DD HH:mm"),
            items: [
              (datum: ChartDatum) => ({ name: datum.metric, value: formatDuration(datum.value) })
            ]
          }}
          legend={{ color: { position: "top" } }}
          height={height}
          autoFit
        />
      </div>
    );
  }

  const span = data.length >= 2 ? data[data.length - 1].bucket - data[0].bucket : 0;
  const policy = tickPolicy(span, variant);
  const values = data.map((item) => item.value);
  const cardYAxis = buildCardYAxisPolicy(values, isSummaryCard ? "summary" : "sparkline");

  return (
    <div className={`latency-chart latency-chart-${variant}`}>
      <PlotLine
        data={data}
        xField="bucket"
        yField="value"
        colorField="metric"
        shapeField="smooth"
        style={{
          lineWidth: (datum: ChartDatum) => (datum.metric === "平均" ? (isCardChart ? 2 : 2.2) : 1.45),
          lineDash: (datum: ChartDatum) => (datum.metric === "P99" ? [4, 3] : undefined),
          opacity: (datum: ChartDatum) => (datum.metric === "平均" ? 1 : isCardChart ? 0.82 : 0.9)
        }}
        point={false}
        paddingTop={isCardChart ? 0 : undefined}
        paddingRight={isCardChart ? 2 : undefined}
        paddingBottom={isCardChart ? 0 : undefined}
        paddingLeft={isCardChart ? 1 : undefined}
        scale={{
          x: { type: "time", nice: !isCardChart },
          y: isCardChart
            ? { domain: [0, cardYAxis.max], tickCount: cardYAxis.ticks.length, nice: false }
            : { nice: true },
          color: { domain: ["平均", "P95", "P99"], range: [CHART_COLORS.avg, CHART_COLORS.p95, CHART_COLORS.p99] }
        }}
        axis={{
          x: {
            tickCount: isCardChart ? (isSummaryCard ? 7 : 6) : policy.tickCount,
            labelFormatter: policy.labelFormatter,
            labelFilter: buildLabelFilter(isCardChart ? Math.max(1, policy.labelStep - 1) : policy.labelStep),
            labelFontSize: 10,
            labelFill: palette.textTertiary,
            labelDirection: isCardChart ? "positive" : undefined,
            labelSpacing: isCardChart ? 0 : undefined,
            labelTransform: isCardChart ? "translate(0, 3)" : "translate(0, 2)",
            labelAutoRotate: false,
            labelAutoHide: true,
            line: true,
            lineStroke: palette.border,
            grid: isCardChart,
            gridStroke: CHART_COLORS.grid,
            gridLineDash: [3, 3],
            tick: isCardChart,
            tickStroke: palette.border,
            tickLength: isCardChart ? 3 : undefined
          },
          y: {
            tickCount: isCardChart ? cardYAxis.ticks.length : undefined,
            tickMethod: isCardChart ? (() => cardYAxis.ticks) : undefined,
            labelFormatter: isCardChart ? (value: number) => String(value) : (value: number) => formatDuration(value),
            labelFontSize: 10,
            labelFill: palette.textTertiary,
            labelDirection: isCardChart ? "positive" : undefined,
            labelSpacing: isCardChart ? 0 : undefined,
            labelTextAlign: isCardChart ? "left" : undefined,
            labelTextBaseline: "middle",
            labelTransform: isCardChart ? "translate(10, 0)" : undefined,
            grid: true,
            gridStroke: CHART_COLORS.grid,
            gridLineDash: [3, 3],
            line: isCardChart,
            lineStroke: palette.border,
            tick: isCardChart,
            tickStroke: palette.border,
            tickLength: isCardChart ? 3 : undefined
          }
        }}
        tooltip={{
          title: (datum: { bucket: number }) => dayjs(datum.bucket).format("MM-DD HH:mm"),
          css: {
            zIndex: 24
          },
          items: [
            (datum: ChartDatum) => ({ name: datum.metric, value: formatDuration(datum.value) })
          ]
        }}
        legend={false}
        height={height}
        autoFit
      />
    </div>
  );
}
