// charts/index.tsx — hand-rolled SVG charts tuned for the dark console.
// Sparkline · KrTrendChart (the hero) · StepTrajectory · AreaTrend · DistBars

import { useRef, useState, useEffect, useId } from "react";
import { C } from "@/lib/theme";

// ── useMeasure ────────────────────────────────────────────────────────
export function useMeasure(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((es) => {
      for (const e of es) setW(e.contentRect.width);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const niceVals = (vals: (number | null)[]): number[] =>
  vals.filter((v): v is number => v != null && !Number.isNaN(v));

// ── Sparkline ─────────────────────────────────────────────────────────
interface SparklineProps {
  data: (number | null)[];
  color?: string;
  width?: number;
  height?: number;
  fill?: boolean;
  strokeWidth?: number;
}

export function Sparkline({
  data,
  color = C.accent,
  width = 96,
  height = 28,
  fill = true,
  strokeWidth = 1.5,
}: SparklineProps) {
  const uid = useId();
  const gid = `sp-${uid.replace(/:/g, "")}`;
  const vals = niceVals(data);
  if (vals.length < 2) return <svg width={width} height={height} />;
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const x = (i: number) => (i / (data.length - 1)) * width;
  const y = (v: number) => height - 3 - ((v - min) / span) * (height - 6);
  let d = "", area = "";
  data.forEach((v, i) => {
    if (v == null) return;
    const cmd = d ? "L" : "M";
    d += `${cmd}${x(i).toFixed(1)} ${y(v).toFixed(1)} `;
  });
  area =
    `M0 ${height} ` +
    data.map((v, i) => (v == null ? "" : `L${x(i).toFixed(1)} ${y(v).toFixed(1)} `)).join("") +
    `L${width} ${height} Z`;
  return (
    <svg width={width} height={height} style={{ display: "block", overflow: "visible" }}>
      {fill && (
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
      )}
      {fill && <path d={area} fill={`url(#${gid})`} />}
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        cx={x(data.length - 1)}
        cy={y(vals[vals.length - 1])}
        r="2"
        fill={color}
      />
    </svg>
  );
}

// ── KR trend chart (HERO) ─────────────────────────────────────────────
// points: [{idx, value|null}], target line, optional "measurement began" gate.
interface KrPoint {
  idx: number;
  value: number | null;
}

interface KrTrendChartProps {
  points: KrPoint[];
  target: number;
  height?: number;
  color?: string;
  targetLabel?: string;
  yFormat?: (v: number) => string;
  goodUp?: boolean;
}

export function KrTrendChart({
  points,
  target,
  height = 240,
  color = C.accent,
  targetLabel = "KR target",
  yFormat = (v) => Math.round(v * 100) + "%",
  goodUp = true,
}: KrTrendChartProps) {
  const [ref, W] = useMeasure();
  const uid = useId();
  const krFillId = `krfill-${uid.replace(/:/g, "")}`;
  const hatchId = `hatch-${uid.replace(/:/g, "")}`;

  const padL = 44, padR = 16, padT = 18, padB = 26;
  const w = Math.max(W, 260), iw = w - padL - padR, ih = height - padT - padB;

  const vals = niceVals(points.map((p) => p.value)).concat([target]);
  let min = Math.min(...vals), max = Math.max(...vals);
  const pad = (max - min) * 0.18 || 0.05;
  min -= pad;
  max += pad;
  const span = max - min || 1;

  const X = (i: number) => padL + (i / (points.length - 1)) * iw;
  const Y = (v: number) => padT + ih - ((v - min) / span) * ih;

  const firstMeasured = points.findIndex((p) => p.value != null);
  const measured = points.filter((p): p is { idx: number; value: number } => p.value != null);

  let line = "", area = "";
  points.forEach((p) => {
    if (p.value == null) return;
    const cmd = line ? "L" : "M";
    line += `${cmd}${X(p.idx).toFixed(1)} ${Y(p.value).toFixed(1)} `;
  });
  if (measured.length) {
    area =
      `M${X(measured[0].idx).toFixed(1)} ${padT + ih} ` +
      measured.map((p) => `L${X(p.idx).toFixed(1)} ${Y(p.value).toFixed(1)} `).join("") +
      `L${X(measured[measured.length - 1].idx).toFixed(1)} ${padT + ih} Z`;
  }

  const cur = measured[measured.length - 1];
  const yTicks = 4;
  const reachedTarget = cur != null && (goodUp ? cur.value >= target : cur.value <= target);
  // reachedTarget used for future visual state — suppress unused warning
  void reachedTarget;

  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={height} style={{ display: "block", overflow: "visible" }}>
        <defs>
          <linearGradient id={krFillId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.20" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
          <pattern id={hatchId} width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="6" height="6" fill="transparent" />
            <line x1="0" y1="0" x2="0" y2="6" stroke={C.textFaint} strokeWidth="1" opacity="0.5" />
          </pattern>
        </defs>

        {/* gridlines + y labels */}
        {Array.from({ length: yTicks + 1 }).map((_, i) => {
          const v = min + (span * i) / yTicks;
          const yy = Y(v);
          return (
            <g key={i}>
              <line x1={padL} y1={yy} x2={w - padR} y2={yy} stroke={C.hairline} />
              <text
                x={padL - 8}
                y={yy + 3}
                textAnchor="end"
                fontSize="10"
                fill={C.textDim}
                fontFamily="var(--mono)"
              >
                {yFormat(v)}
              </text>
            </g>
          );
        })}

        {/* not-yet-measured zone */}
        {firstMeasured > 0 && (
          <g>
            <rect
              x={padL}
              y={padT}
              width={X(firstMeasured) - padL}
              height={ih}
              fill={`url(#${hatchId})`}
              opacity="0.5"
            />
            <text
              x={(padL + X(firstMeasured)) / 2}
              y={padT + ih / 2}
              textAnchor="middle"
              fontSize="9.5"
              fill={C.textDim}
              fontFamily="var(--mono)"
            >
              not yet measured
            </text>
          </g>
        )}

        {/* target line */}
        <line
          x1={padL}
          y1={Y(target)}
          x2={w - padR}
          y2={Y(target)}
          stroke={C.emerald}
          strokeWidth="1.5"
          strokeDasharray="5 4"
          opacity="0.9"
        />
        <rect
          x={w - padR - 92}
          y={Y(target) - 18}
          width="92"
          height="15"
          rx="3"
          fill={C.emerald}
          opacity="0.13"
        />
        <text
          x={w - padR - 6}
          y={Y(target) - 7}
          textAnchor="end"
          fontSize="10"
          fill={C.emerald}
          fontFamily="var(--mono)"
          fontWeight="600"
        >
          {targetLabel} {yFormat(target)}
        </text>

        {/* area + line */}
        {area && <path d={area} fill={`url(#${krFillId})`} />}
        <path
          d={line}
          fill="none"
          stroke={color}
          strokeWidth="2.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* measurement-began marker */}
        {firstMeasured > 0 && (
          <line
            x1={X(firstMeasured)}
            y1={padT}
            x2={X(firstMeasured)}
            y2={padT + ih}
            stroke={C.textFaint}
            strokeDasharray="2 3"
          />
        )}

        {/* current value */}
        {cur != null && (
          <g>
            <circle cx={X(cur.idx)} cy={Y(cur.value)} r="8" fill={color} opacity="0.18">
              <animate attributeName="r" values="6;11;6" dur="2.4s" repeatCount="indefinite" />
              <animate attributeName="opacity" values="0.22;0.05;0.22" dur="2.4s" repeatCount="indefinite" />
            </circle>
            <circle
              cx={X(cur.idx)}
              cy={Y(cur.value)}
              r="4"
              fill={color}
              stroke={C.bg}
              strokeWidth="1.5"
            />
            <text
              x={X(cur.idx) - 8}
              y={Y(cur.value) - 10}
              textAnchor="end"
              fontSize="12"
              fill={C.textHi}
              fontFamily="var(--mono)"
              fontWeight="700"
            >
              {yFormat(cur.value)}
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}

// ── Step trajectory (repair rounds: sev2 across rounds) ───────────────
interface StepTrajectoryEntry {
  round: number;
  sev2: number;
}

interface StepTrajectoryProps {
  history: StepTrajectoryEntry[];
  height?: number;
  width?: number;
}

export function StepTrajectory({ history, height = 86, width = 200 }: StepTrajectoryProps) {
  if (!history || history.length === 0) return null;
  const vals = history.map((h) => h.sev2);
  const max = Math.max(...vals, 1);
  const padB = 18, padT = 8, iw = width - 20, ih = height - padB - padT;
  const X = (i: number) =>
    12 + (history.length === 1 ? iw / 2 : (i / (history.length - 1)) * iw);
  const Y = (v: number) => padT + ih - (v / max) * ih;
  const converged = vals[vals.length - 1] === 0;
  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      <line x1="12" y1={padT + ih} x2={12 + iw} y2={padT + ih} stroke={C.hairline} />
      <path
        d={history.map((h, i) => `${i ? "L" : "M"}${X(i)} ${Y(h.sev2)}`).join(" ")}
        fill="none"
        stroke={converged ? C.emerald : C.amber}
        strokeWidth="2"
      />
      {history.map((h, i) => (
        <g key={i}>
          <circle
            cx={X(i)}
            cy={Y(h.sev2)}
            r="3.5"
            fill={i === history.length - 1 ? (converged ? C.emerald : C.amber) : C.elevated}
            stroke={converged && i === history.length - 1 ? C.emerald : C.amber}
            strokeWidth="1.5"
          />
          <text
            x={X(i)}
            y={Y(h.sev2) - 8}
            textAnchor="middle"
            fontSize="10"
            fontFamily="var(--mono)"
            fill={C.textMid}
            fontWeight="600"
          >
            {h.sev2}
          </text>
          <text
            x={X(i)}
            y={height - 4}
            textAnchor="middle"
            fontSize="9"
            fontFamily="var(--mono)"
            fill={C.textDim}
          >
            r{h.round}
          </text>
        </g>
      ))}
    </svg>
  );
}

// ── Area trend (budget) ───────────────────────────────────────────────
interface AreaTrendProps<T> {
  points: T[];
  accessor: (p: T) => number;
  color?: string;
  height?: number;
  yFormat?: (v: number) => string | number;
}

export function AreaTrend<T>({
  points,
  accessor,
  color = C.accent,
  height = 150,
  yFormat = (v) => v,
}: AreaTrendProps<T>) {
  const [ref, W] = useMeasure();
  const uid = useId();
  const atFillId = `atfill-${uid.replace(/:/g, "")}`;

  const padL = 48, padR = 12, padT = 12, padB = 22;
  const w = Math.max(W, 240), iw = w - padL - padR, ih = height - padT - padB;
  const vals = points.map(accessor);
  const min = Math.min(...vals) * 0.96, max = Math.max(...vals) * 1.04, span = max - min || 1;
  const X = (i: number) => padL + (i / (points.length - 1)) * iw;
  const Y = (v: number) => padT + ih - ((v - min) / span) * ih;
  const line = points
    .map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(accessor(p)).toFixed(1)}`)
    .join(" ");
  const area =
    `M${padL} ${padT + ih} ` +
    points.map((p, i) => `L${X(i).toFixed(1)} ${Y(accessor(p)).toFixed(1)}`).join(" ") +
    ` L${X(points.length - 1)} ${padT + ih} Z`;
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width={w} height={height} style={{ display: "block" }}>
        <defs>
          <linearGradient id={atFillId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 0.5, 1].map((f, i) => {
          const v = min + span * f;
          const yy = Y(v);
          return (
            <g key={i}>
              <line x1={padL} y1={yy} x2={w - padR} y2={yy} stroke={C.hairline} />
              <text
                x={padL - 8}
                y={yy + 3}
                textAnchor="end"
                fontSize="10"
                fill={C.textDim}
                fontFamily="var(--mono)"
              >
                {yFormat(v)}
              </text>
            </g>
          );
        })}
        <path d={area} fill={`url(#${atFillId})`} />
        <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
        <circle
          cx={X(points.length - 1)}
          cy={Y(accessor(points[points.length - 1]))}
          r="3"
          fill={color}
        />
      </svg>
    </div>
  );
}

// ── Distribution bars (axes) ──────────────────────────────────────────
interface DistBarsRow {
  label: string;
  color: string;
  value: number;
}

interface DistBarsProps {
  rows: DistBarsRow[];
  height?: number;
}

export function DistBars({ rows, height = 10 }: DistBarsProps) {
  const total = rows.reduce((s, r) => s + r.value, 0) || 1;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        style={{
          display: "flex",
          height,
          borderRadius: 4,
          overflow: "hidden",
          background: C.elevated,
        }}
      >
        {rows.map((r) => (
          <div
            key={r.label}
            title={`${r.label}: ${r.value}`}
            style={{ width: `${(r.value / total) * 100}%`, background: r.color }}
          />
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 18px" }}>
        {rows.map((r) => (
          <div
            key={r.label}
            style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: 2,
                background: r.color,
                flexShrink: 0,
              }}
            />
            <span
              style={{
                color: C.textMid,
                flex: 1,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {r.label}
            </span>
            <span style={{ color: C.textHi, fontFamily: "var(--mono)", fontWeight: 600 }}>
              {r.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
