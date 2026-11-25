/** components/common/LoadingCurve.tsx — Phase 50-H: 数学曲线加载动画
 *
 * 基于 math-curve-loaders (https://github.com/Paidax01/math-curve-loaders)
 * 的 SVG + requestAnimationFrame 实现。保留简单圆圈 spinner，同时提供
 * 玫瑰曲线、李萨如曲线、内摆线、心形线、双纽线、傅里叶曲线、蝴蝶曲线等
 * 数学曲线变体。
 */
import { useEffect, useRef } from "react";
import { getCurve, buildPath, getParticle, type CurveVariant } from "./mathCurves";

interface LoadingCurveProps {
  variant?: CurveVariant;
  size?: number;
  speed?: number;
  particleCount?: number;
  className?: string;
  color?: string;
}

export function LoadingCurve({
  variant = "rose",
  size = 120,
  speed = 1,
  particleCount: particleCountProp,
  className = "",
  color = "currentColor",
}: LoadingCurveProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const rafRef = useRef<number | null>(null);
  const startRef = useRef<number>(performance.now());

  const curve = getCurve(variant);
  const isSpinner = !curve;

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;

    if (isSpinner) {
      return;
    }

    const config = curve;
    const particleCount = Math.min(particleCountProp ?? config.particleCount, config.particleCount);
    const pathEl = svg.querySelector("path");
    const groupEl = svg.querySelector("g");
    const circles = Array.from(svg.querySelectorAll("circle"));
    startRef.current = performance.now();

    const animate = (now: number) => {
      const time = (now - startRef.current) * speed;
      const progress = (time % config.durationMs) / config.durationMs;
      const pulseAngle = ((time % config.pulseDurationMs) / config.pulseDurationMs) * Math.PI * 2;
      const detailScale = 0.52 + ((Math.sin(pulseAngle + 0.55) + 1) / 2) * 0.48;
      const rotation = config.rotate ? -((time % config.rotationDurationMs) / config.rotationDurationMs) * 360 : 0;

      if (groupEl) {
        groupEl.setAttribute("transform", `rotate(${rotation} 50 50)`);
      }
      if (pathEl) {
        pathEl.setAttribute("d", buildPath(config, detailScale));
      }

      circles.forEach((c, i) => {
        if (i >= particleCount) {
          c.setAttribute("opacity", "0");
          return;
        }
        const p = getParticle(config, i, progress, detailScale);
        c.setAttribute("cx", String(p.x));
        c.setAttribute("cy", String(p.y));
        c.setAttribute("r", String(p.radius));
        c.setAttribute("opacity", String(p.opacity));
      });

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [curve, isSpinner, speed, particleCountProp]);

  if (isSpinner) {
    return (
      <svg
        ref={svgRef}
        width={size}
        height={size}
        viewBox="0 0 24 24"
        className={`animate-spin ${className}`}
        style={{ color }}
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
        <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
      </svg>
    );
  }

  const particleCount = Math.min(particleCountProp ?? curve.particleCount, curve.particleCount);

  return (
    <svg
      ref={svgRef}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="none"
      className={className}
      style={{ color }}
      aria-hidden="true"
    >
      <g>
        <path
          stroke="currentColor"
          strokeWidth={curve.strokeWidth}
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity={0.1}
        />
        {Array.from({ length: particleCount }).map((_, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <circle key={i} fill="currentColor" />
        ))}
      </g>
    </svg>
  );
}
