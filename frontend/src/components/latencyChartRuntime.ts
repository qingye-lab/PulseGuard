import { Runtime } from "@antv/g2/esm/api/runtime";
import { extend } from "@antv/g2/esm/api/extend";
import { Cartesian } from "@antv/g2/esm/coordinate/index";
import { Constant, Field, Transform, Column } from "@antv/g2/esm/encode/index";
import { Line, Point } from "@antv/g2/esm/mark/index";
import { Inline } from "@antv/g2/esm/data/inline";
import { Category10, Category20 } from "@antv/g2/esm/palette/index";
import { Linear, Ordinal, Identity, Time, Band, Sqrt, Point as PointScale, Constant as ConstantScale } from "@antv/g2/esm/scale/index";
import { Classic, Light } from "@antv/g2/esm/theme/index";
import { AxisX, AxisY, LegendCategory, Legends } from "@antv/g2/esm/component/index";
import { FadeIn, FadeOut, Morphing } from "@antv/g2/esm/animation/index";
import { Tooltip, LegendFilter, LegendHighlight, SliderFilter, ScrollbarFilter } from "@antv/g2/esm/interaction/index";

// G2's supported library extension boundary. This business chart only needs
// Cartesian time-series lines and optional points; no statistics/geo/graph
// registry should be shipped for it. Keep axis, tooltip and legend behavior
// implemented by AntV instead of recreating canvas or interaction primitives.
// https://g2.antv.antgroup.com/en/manual/extra-topics/bundle
export const LatencyRuntime = extend(Runtime, {
  "data.inline": Inline,
  "coordinate.cartesian": Cartesian,
  "encode.constant": Constant,
  "encode.field": Field,
  "encode.transform": Transform,
  "encode.column": Column,
  "mark.line": Line,
  "mark.point": Point,
  "palette.category10": Category10,
  "palette.category20": Category20,
  "scale.linear": Linear,
  "scale.ordinal": Ordinal,
  "scale.identity": Identity,
  "scale.time": Time,
  "scale.band": Band,
  "scale.point": PointScale,
  "scale.sqrt": Sqrt,
  "scale.constant": ConstantScale,
  "theme.classic": Classic,
  "theme.light": Light,
  "component.axisX": AxisX,
  "component.axisY": AxisY,
  "component.legendCategory": LegendCategory,
  "component.legends": Legends,
  "animation.fadeIn": FadeIn,
  "animation.fadeOut": FadeOut,
  "animation.morphing": Morphing,
  "interaction.tooltip": Tooltip,
  "interaction.legendFilter": LegendFilter,
  "interaction.legendHighlight": LegendHighlight,
  "interaction.sliderFilter": SliderFilter,
  "interaction.scrollbarFilter": ScrollbarFilter
});
