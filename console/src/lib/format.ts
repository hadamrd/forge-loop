// src/lib/format.ts — formatters used across the UI. Pure, no React.
import { color } from "./theme";

export function relativeTime(iso: string | number, now = Date.now()): string {
  const d = typeof iso === "number" ? iso : new Date(iso).getTime();
  const s = Math.round((now - d) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function duration(seconds: number | null): string {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60; if (m < 60) return rs ? `${m}m ${rs}s` : `${m}m`;
  const h = Math.floor(m / 60), rm = m % 60; return rm ? `${h}h ${rm}m` : `${h}h`;
}

export const money = (n: number | null, dp = 2) =>
  n == null ? "—" : "$" + n.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });

export function compact(n: number | null): string {
  if (n == null) return "—";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}

export const pct = (n: number | null, dp = 0) => (n == null ? "—" : (n * 100).toFixed(dp) + "%");

/** Heartbeat freshness → color + label. Drives the live/stale/expired dots. */
export function freshness(iso: string, now = Date.now()) {
  const age = (now - new Date(iso).getTime()) / 1000;
  if (age < 30) return { color: color.emerald, label: "live", age };
  if (age < 90) return { color: color.amber, label: "stale", age };
  return { color: color.red, label: "expired", age };
}

/** Wall-clock HH:MM:SS (24h) — used in drawer headers. */
export function clockTime(iso: string | number): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
