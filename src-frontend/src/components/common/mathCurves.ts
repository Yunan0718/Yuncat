/** components/common/mathCurves.ts —— 数学曲线加载动画定义
 *
 * 从 math-curve-loaders (https://github.com/Paidax01/math-curve-loaders)
 * 提炼的曲线参数化定义，适配 React + SVG + requestAnimationFrame。
 *
 * 坐标系：viewBox 0..100，中心 (50, 50)。
 */

export interface CurveConfig {
  name: string;
  particleCount: number;
  trailSpan: number;
  durationMs: number;
  rotationDurationMs: number;
  pulseDurationMs: number;
  strokeWidth: number;
  rotate: boolean;
  point(progress: number, detailScale: number, config: CurveConfig): { x: number; y: number };
}

function normalizeProgress(p: number): number {
  return ((p % 1) + 1) % 1;
}

export const MATH_CURVES: Record<string, CurveConfig> = {
  rose: {
    name: "Rose Curve",
    particleCount: 48,
    trailSpan: 0.32,
    durationMs: 5400,
    rotationDurationMs: 28000,
    pulseDurationMs: 4600,
    strokeWidth: 4.5,
    rotate: true,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const a = 9.2 + detailScale * 0.6;
      const k = 5;
      const r = a * (0.72 + detailScale * 0.28) * Math.cos(k * t);
      return {
        x: 50 + Math.cos(t) * r * 3.25,
        y: 50 + Math.sin(t) * r * 3.25,
      };
    },
  },

  lissajous: {
    name: "Lissajous",
    particleCount: 44,
    trailSpan: 0.34,
    durationMs: 6000,
    rotationDurationMs: 36000,
    pulseDurationMs: 5400,
    strokeWidth: 4.7,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const amp = 24 + detailScale * 6;
      return {
        x: 50 + Math.sin(3 * t + 1.57) * amp,
        y: 50 + Math.sin(4 * t) * (amp * 0.92),
      };
    },
  },

  hypotrochoid: {
    name: "Hypotrochoid",
    particleCount: 52,
    trailSpan: 0.46,
    durationMs: 7600,
    rotationDurationMs: 42000,
    pulseDurationMs: 6200,
    strokeWidth: 4.6,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const R = 8.2;
      const r = 2.7 + detailScale * 0.45;
      const d = 4.8 + detailScale * 1.2;
      const x = (R - r) * Math.cos(t) + d * Math.cos(((R - r) / r) * t);
      const y = (R - r) * Math.sin(t) - d * Math.sin(((R - r) / r) * t);
      return {
        x: 50 + x * 3.05,
        y: 50 + y * 3.05,
      };
    },
  },

  cardioid: {
    name: "Cardioid",
    particleCount: 44,
    trailSpan: 0.36,
    durationMs: 6200,
    rotationDurationMs: 36000,
    pulseDurationMs: 5200,
    strokeWidth: 4.9,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const a = 8.4 + detailScale * 0.8;
      const r = a * (1 - Math.cos(t));
      return {
        x: 50 + Math.cos(t) * r * 2.15,
        y: 50 + Math.sin(t) * r * 2.15,
      };
    },
  },

  lemniscate: {
    name: "Lemniscate",
    particleCount: 46,
    trailSpan: 0.4,
    durationMs: 5600,
    rotationDurationMs: 34000,
    pulseDurationMs: 5000,
    strokeWidth: 4.8,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const scale = 20 + detailScale * 7;
      const denom = 1 + Math.sin(t) ** 2;
      return {
        x: 50 + (scale * Math.cos(t)) / denom,
        y: 50 + (scale * Math.sin(t) * Math.cos(t)) / denom,
      };
    },
  },

  fourier: {
    name: "Fourier Flow",
    particleCount: 54,
    trailSpan: 0.31,
    durationMs: 8400,
    rotationDurationMs: 44000,
    pulseDurationMs: 6800,
    strokeWidth: 4.2,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 2;
      const mix = 1 + detailScale * 0.16;
      const x = 17 * Math.cos(t) + 7.5 * Math.cos(3 * t + 0.6 * mix) + 3.2 * Math.sin(5 * t - 0.4);
      const y = 15 * Math.sin(t) + 8.2 * Math.sin(2 * t + 0.25) - 4.2 * Math.cos(4 * t - 0.5 * mix);
      return { x: 50 + x, y: 50 + y };
    },
  },

  butterfly: {
    name: "Butterfly",
    particleCount: 56,
    trailSpan: 0.32,
    durationMs: 9000,
    rotationDurationMs: 50000,
    pulseDurationMs: 7000,
    strokeWidth: 4.4,
    rotate: false,
    point(progress, detailScale) {
      const t = progress * Math.PI * 12;
      const s = Math.exp(Math.cos(t)) - 2 * Math.cos(4 * t) - Math.sin(t / 12) ** 5;
      const scale = 4.6 + detailScale * 0.45;
      return {
        x: 50 + Math.sin(t) * s * scale,
        y: 50 + Math.cos(t) * s * scale,
      };
    },
  },
};

export type CurveVariant = "spinner" | keyof typeof MATH_CURVES;

export function getCurve(variant: CurveVariant): CurveConfig | null {
  if (variant === "spinner") return null;
  return MATH_CURVES[variant] ?? MATH_CURVES.rose;
}

export function getParticle(
  config: CurveConfig,
  index: number,
  progress: number,
  detailScale: number,
): { x: number; y: number; radius: number; opacity: number } {
  const tailOffset = index / (config.particleCount - 1);
  const point = config.point(normalizeProgress(progress - tailOffset * config.trailSpan), detailScale, config);
  const fade = Math.pow(1 - tailOffset, 0.56);
  return {
    x: point.x,
    y: point.y,
    radius: 0.9 + fade * 2.7,
    opacity: 0.04 + fade * 0.96,
  };
}

export function buildPath(config: CurveConfig, detailScale: number, steps = 240): string {
  return Array.from({ length: steps + 1 }, (_, i) => {
    const p = config.point(i / steps, detailScale, config);
    return `${i === 0 ? "M" : "L"} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`;
  }).join(" ");
}
