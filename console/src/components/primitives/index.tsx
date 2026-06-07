// src/components/primitives/index.tsx
// Presentational primitives — pixel-identical port of prototype/ui.jsx.
// No data fetching. All styling via existing index.css classes + inline styles.

import { useState, useMemo, useEffect, type ReactNode, type CSSProperties } from "react";
import { Icon } from "@/components/Icon";
import { C, eventMeta, sevMeta, sagaMeta, labelMeta, axisMeta } from "@/lib/theme";
import { Sparkline } from "@/components/charts";

// ── Panel / card ────────────────────────────────────────────────────────────

export interface PanelProps {
  title?: ReactNode;
  icon?: string;
  action?: ReactNode;
  children?: ReactNode;
  pad?: boolean;
  style?: CSSProperties;
  bodyStyle?: CSSProperties;
  dense?: boolean;
}

export function Panel({ title, icon, action, children, pad = true, style, bodyStyle, dense }: PanelProps) {
  return (
    <section className="panel" style={style}>
      {(title || action) && (
        <header className="panel-h">
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            {icon && <Icon name={icon} size={14} color={C.textMid} />}
            <h3 className="panel-t">{title}</h3>
          </div>
          {action}
        </header>
      )}
      <div style={{ padding: pad ? (dense ? "10px 14px 14px" : "14px 16px 16px") : 0, ...bodyStyle }}>
        {children}
      </div>
    </section>
  );
}

// ── Generic status pill (icon + label + color) ───────────────────────────────

export interface PillProps {
  icon?: string;
  color?: string;
  children?: ReactNode;
  pulse?: boolean;
  size?: "sm" | "md";
  solid?: boolean;
  title?: string;
}

export function Pill({ icon, color = C.textMid, children, pulse, size = "md", solid, title }: PillProps) {
  const h = size === "sm" ? 20 : 24;
  const fs = size === "sm" ? 11 : 12;
  const isz = size === "sm" ? 11 : 13;
  return (
    <span
      title={title}
      className="pill"
      style={{
        height: h,
        fontSize: fs,
        color: solid ? C.bg : color,
        paddingInline: size === "sm" ? 7 : 9,
        background: solid ? color : `color-mix(in oklab, ${color} 14%, transparent)`,
        borderColor: `color-mix(in oklab, ${color} 30%, transparent)`,
      }}
    >
      {pulse && <span className="dot-pulse" style={{ background: color }} />}
      {icon && !pulse && <Icon name={icon} size={isz} color={solid ? C.bg : color} />}
      {children}
    </span>
  );
}

// ── Composite badges ─────────────────────────────────────────────────────────

export interface KindBadgeProps {
  kind: string;
  size?: "sm" | "md";
  showLabel?: boolean;
}

export function KindBadge({ kind, size = "md", showLabel = true }: KindBadgeProps) {
  const m = eventMeta(kind);
  return (
    <Pill icon={m.icon} color={m.color} size={size}>
      {showLabel ? m.label : null}
    </Pill>
  );
}

export interface SagaStateBadgeProps {
  state: string;
  size?: "sm" | "md";
}

export function SagaStateBadge({ state, size = "md" }: SagaStateBadgeProps) {
  const m = sagaMeta(state);
  return (
    <Pill icon={m.icon} color={m.color} pulse={m.pulse} size={size}>
      {m.label}
    </Pill>
  );
}

export interface SevBadgeProps {
  sev: string;
  count?: number | null;
  size?: "sm" | "md";
}

export function SevBadge({ sev, count, size = "sm" }: SevBadgeProps) {
  const m = sevMeta(sev);
  return (
    <Pill icon={m.icon} color={m.color} size={size}>
      {count != null ? `${count} ` : ""}
      {m.label.replace("Sev ", "S")}
    </Pill>
  );
}

export interface LabelChipProps {
  label: string;
  size?: "sm" | "md";
}

export function LabelChip({ label, size = "sm" }: LabelChipProps) {
  const m = labelMeta(label);
  return (
    <Pill icon={m.icon} color={m.color} size={size}>
      {m.label}
    </Pill>
  );
}

export interface AxisChipProps {
  axis: string;
  size?: "sm" | "md";
  short?: boolean;
}

export function AxisChip({ axis, size = "sm", short }: AxisChipProps) {
  const m = axisMeta(axis);
  return (
    <span
      className="pill"
      style={{
        height: size === "sm" ? 20 : 24,
        fontSize: size === "sm" ? 11 : 12,
        color: m.color,
        paddingInline: 8,
        background: `color-mix(in oklab, ${m.color} 12%, transparent)`,
        borderColor: `color-mix(in oklab, ${m.color} 26%, transparent)`,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: 2, background: m.color }} />
      {short ? m.short : m.label}
    </span>
  );
}

// ── Trend arrow ──────────────────────────────────────────────────────────────

export interface TrendArrowProps {
  delta?: number | null;
  goodUp?: boolean;
  suffix?: string;
}

export function TrendArrow({ delta, goodUp = true, suffix = "" }: TrendArrowProps) {
  if (delta == null || delta === 0) {
    return <span style={{ color: C.textDim, fontFamily: "var(--mono)", fontSize: 12 }}>–</span>;
  }
  const up = delta > 0;
  const good = up === goodUp;
  const color = good ? C.emerald : C.red;
  return (
    <span
      style={{
        color,
        fontFamily: "var(--mono)",
        fontSize: 12,
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        fontWeight: 600,
      }}
    >
      <Icon name={up ? "TrendingUp" : "TrendingDown"} size={13} color={color} />
      {Math.abs(delta)}
      {suffix}
    </span>
  );
}

// ── KPI card ─────────────────────────────────────────────────────────────────

export interface KpiProps {
  label: ReactNode;
  value: ReactNode;
  unit?: ReactNode;
  sub?: ReactNode;
  icon?: string;
  color?: string;
  spark?: (number | null)[];
  sparkColor?: string;
  trend?: ReactNode;
  onClick?: () => void;
  accent?: boolean;
}

export function Kpi({ label, value, unit, sub, icon, color = C.textMid, spark, sparkColor, trend, onClick, accent }: KpiProps) {
  return (
    <div
      className={"kpi" + (onClick ? " kpi-click" : "")}
      onClick={onClick}
      style={accent ? { borderColor: `color-mix(in oklab, ${color} 32%, ${C.border})` } : undefined}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span className="kpi-l">
          {icon && <Icon name={icon} size={13} color={color} />}
          {label}
        </span>
        {trend !== undefined && trend}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: 8,
          marginTop: 6,
        }}
      >
        <div className="kpi-v">
          {value}
          {unit && <span className="kpi-u">{unit}</span>}
        </div>
        {spark && <Sparkline data={spark} color={sparkColor ?? color} width={88} height={30} />}
      </div>
      {sub && <div className="kpi-s">{sub}</div>}
    </div>
  );
}

// ── Alert banner ──────────────────────────────────────────────────────────────

export type BannerTone = "info" | "good" | "warn" | "bad";

export interface BannerProps {
  tone?: BannerTone;
  icon?: string;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}

export function Banner({ tone = "info", icon, title, children, action }: BannerProps) {
  const map: Record<BannerTone, string> = {
    info: C.accent,
    good: C.emerald,
    warn: C.amber,
    bad: C.red,
  };
  const color = map[tone] ?? C.accent;
  return (
    <div
      className="banner"
      style={{
        borderColor: `color-mix(in oklab, ${color} 38%, transparent)`,
        background: `color-mix(in oklab, ${color} 9%, ${C.panel})`,
      }}
    >
      <span className="banner-bar" style={{ background: color }} />
      {icon && <Icon name={icon} size={16} color={color} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ color: C.textHi, fontWeight: 600, fontSize: 13 }}>{title}</div>
        {children && (
          <div style={{ color: C.textMid, fontSize: 12.5, marginTop: 2 }}>{children}</div>
        )}
      </div>
      {action}
    </div>
  );
}

// ── Empty state ───────────────────────────────────────────────────────────────

export interface EmptyStateProps {
  icon?: string;
  color?: string;
  title?: ReactNode;
  sub?: ReactNode;
}

export function EmptyState({ icon = "CircleCheck", color = C.emerald, title, sub }: EmptyStateProps) {
  return (
    <div className="empty">
      <div
        className="empty-ic"
        style={{ color, background: `color-mix(in oklab, ${color} 12%, transparent)` }}
      >
        <Icon name={icon} size={20} color={color} />
      </div>
      <div style={{ color: C.textHi, fontWeight: 600, fontSize: 13.5 }}>{title}</div>
      {sub && (
        <div style={{ color: C.textDim, fontSize: 12.5, maxWidth: 320, textAlign: "center" }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// ── Not-yet-measured tile ─────────────────────────────────────────────────────

export interface NotMeasuredProps {
  label?: ReactNode;
  reason?: ReactNode;
  detail?: ReactNode;
}

export function NotMeasured({ label, reason, detail }: NotMeasuredProps) {
  return (
    <div className="kpi" style={{ borderStyle: "dashed", borderColor: C.border }}>
      <span className="kpi-l">
        <Icon name="CircleDashed" size={13} color={C.textDim} />
        {label}
      </span>
      <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 7 }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 18, color: C.textDim }}>—</span>
        <span style={{ fontSize: 11.5, color: C.amber, fontWeight: 600, letterSpacing: 0.2 }}>
          {reason}
        </span>
      </div>
      {detail && <div className="kpi-s" style={{ color: C.textDim }}>{detail}</div>}
    </div>
  );
}

// ── Right-side drawer ─────────────────────────────────────────────────────────

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  children?: ReactNode;
  width?: number;
  label?: string;
}

export function Drawer({ open, onClose, children, width = 520, label }: DrawerProps) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (open) window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);

  return (
    <>
      <div className={"scrim" + (open ? " on" : "")} onClick={onClose} />
      <aside className={"drawer" + (open ? " on" : "")} style={{ width }} aria-label={label} role="dialog">
        {open && children}
      </aside>
    </>
  );
}

export interface DrawerHeaderProps {
  icon?: string;
  color?: string;
  kicker?: ReactNode;
  title?: ReactNode;
  onClose: () => void;
  right?: ReactNode;
}

export function DrawerHeader({ icon, color, kicker, title, onClose, right }: DrawerHeaderProps) {
  return (
    <header className="drawer-h">
      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        {icon && color && (
          <span
            className="drawer-ic"
            style={{ color, background: `color-mix(in oklab, ${color} 14%, transparent)` }}
          >
            <Icon name={icon} size={16} color={color} />
          </span>
        )}
        <div style={{ minWidth: 0 }}>
          {kicker && <div className="drawer-kick">{kicker}</div>}
          <div className="drawer-t">{title}</div>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        {right}
        <button className="icon-btn" onClick={onClose} title="Close (Esc)">
          <Icon name="X" size={16} color={C.textMid} />
        </button>
      </div>
    </header>
  );
}

// ── Sortable data table ───────────────────────────────────────────────────────

export interface ColumnDef<T> {
  id: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  sortVal?: (row: T) => string | number;
  align?: "left" | "right";
  width?: number | string;
}

export interface SortState {
  id: string;
  dir: "asc" | "desc";
}

export interface DataTableProps<T> {
  columns: ColumnDef<T>[];
  rows: T[];
  onRow?: (row: T) => void;
  rowKey?: (row: T, i: number) => string | number;
  initialSort?: SortState;
  empty?: ReactNode;
  dense?: boolean;
}

export function DataTable<T>({
  columns,
  rows,
  onRow,
  rowKey = (_r: T, i: number) => i,
  initialSort,
  empty,
  dense,
}: DataTableProps<T>) {
  const [sort, setSort] = useState<SortState | null>(initialSort ?? null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.id === sort.id);
    if (!col?.sortVal) return rows;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = col.sortVal!(a);
      const bv = col.sortVal!(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [rows, sort, columns]);

  const toggle = (id: string) =>
    setSort((s) =>
      s && s.id === id
        ? { id, dir: s.dir === "asc" ? "desc" : "asc" }
        : { id, dir: "asc" }
    );

  if (rows.length === 0 && empty) return <>{empty}</>;

  return (
    <div className="tbl-wrap">
      <table className={"tbl" + (dense ? " tbl-dense" : "")}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.id}
                style={{ width: c.width, textAlign: c.align ?? "left" }}
                className={c.sortVal ? "th-sort" : ""}
                onClick={c.sortVal ? () => toggle(c.id) : undefined}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                    justifyContent: c.align === "right" ? "flex-end" : "flex-start",
                  }}
                >
                  {c.header}
                  {sort && sort.id === c.id && (
                    <Icon
                      name={sort.dir === "asc" ? "ChevronUp" : "ChevronDown"}
                      size={12}
                      color={C.textMid}
                    />
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr
              key={rowKey(r, i)}
              className={onRow ? "tr-click" : ""}
              onClick={onRow ? () => onRow(r) : undefined}
            >
              {columns.map((c) => (
                <td key={c.id} style={{ textAlign: c.align ?? "left" }}>
                  {c.cell(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

export interface TabItem {
  id: string;
  label: ReactNode;
  icon?: string;
  count?: number | null;
}

export interface TabsProps {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
}

export function Tabs({ tabs, value, onChange }: TabsProps) {
  return (
    <div className="tabs">
      {tabs.map((t) => (
        <button
          key={t.id}
          className={"tab" + (value === t.id ? " on" : "")}
          onClick={() => onChange(t.id)}
        >
          {t.icon && (
            <Icon name={t.icon} size={13} color={value === t.id ? C.textHi : C.textDim} />
          )}
          {t.label}
          {t.count != null && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

// ── Section heading ───────────────────────────────────────────────────────────

export interface SectionLabelProps {
  children?: ReactNode;
  right?: ReactNode;
}

export function SectionLabel({ children, right }: SectionLabelProps) {
  return (
    <div className="sec-label">
      <span>{children}</span>
      {right}
    </div>
  );
}

// ── KV row ────────────────────────────────────────────────────────────────────

export interface KVProps {
  k: ReactNode;
  children?: ReactNode;
  mono?: boolean;
}

export function KV({ k, children, mono }: KVProps) {
  return (
    <div className="kv">
      <span className="kv-k">{k}</span>
      <span className="kv-v" style={mono ? { fontFamily: "var(--mono)" } : undefined}>
        {children}
      </span>
    </div>
  );
}
