// src/lib/theme.ts
// THE single canonical semantic system. Every EventKind, Severity, SagaState, and PR label gets
// exactly ONE Lucide icon name + ONE color + ONE label — defined here, nowhere else.
// Rule: never color alone. Components always render icon + text alongside color (glanceability + a11y).

import type { EventKind, CriticVerdict } from "../domain/events";
import type { SagaState, Severity, ValueAxis, PrLabel, MemoryKind } from "../domain/models";

export const color = {
  bg: "#070809", panel: "#0c0e11", elevated: "#11141a", raised: "#161a21",
  border: "#1b1f27", hi: "#e7edf3", mid: "#94a1ad", dim: "#5a6671", faint: "#3c454e",
  accent: "#22d3ee", emerald: "#34d399", amber: "#fbbf24", red: "#f87171", violet: "#a78bfa", blue: "#60a5fa", slate: "#64748b",
} as const;

export interface Meta { icon: string; color: string; label: string; }

export const EVENT_META: Record<EventKind, Meta & { cat: string }> = {
  "tick.started": { icon: "Play", color: color.accent, label: "Tick started", cat: "loop" },
  "tick.completed": { icon: "CircleCheck", color: color.dim, label: "Tick completed", cat: "loop" },
  "task.planned": { icon: "ClipboardList", color: color.blue, label: "Task planned", cat: "task" },
  "task.dispatched": { icon: "Send", color: color.accent, label: "Task dispatched", cat: "task" },
  "task.heartbeat": { icon: "Activity", color: color.dim, label: "Heartbeat", cat: "task" },
  "task.completed": { icon: "Check", color: color.emerald, label: "Task completed", cat: "task" },
  "task.failed": { icon: "CircleX", color: color.red, label: "Task failed", cat: "task" },
  "task.compensated": { icon: "Undo2", color: color.amber, label: "Task compensated", cat: "task" },
  "pr.opened": { icon: "GitPullRequest", color: color.blue, label: "PR opened", cat: "pr" },
  "pr.merged": { icon: "GitMerge", color: color.emerald, label: "PR merged", cat: "pr" },
  "merge.blocked": { icon: "ShieldX", color: color.red, label: "Merge blocked", cat: "pr" },
  "critique.issued": { icon: "ScanSearch", color: color.violet, label: "Critique issued", cat: "critic" },
  "worktree.reaped": { icon: "Trash2", color: color.dim, label: "Worktree reaped", cat: "infra" },
  "frontier.advanced": { icon: "Flag", color: color.accent, label: "Frontier advanced", cat: "frontier" },
  "idea.rejected": { icon: "X", color: color.amber, label: "Idea rejected", cat: "frontier" },
  "decision.made": { icon: "GitBranchPlus", color: color.blue, label: "Decision made", cat: "frontier" },
  "memory.promoted": { icon: "BrainCircuit", color: color.emerald, label: "Memory promoted", cat: "memory" },
  "memory.superseded": { icon: "Layers", color: color.dim, label: "Memory superseded", cat: "memory" },
  "compaction.performed": { icon: "Package", color: color.dim, label: "Compaction", cat: "infra" },
  "vision.updated": { icon: "Telescope", color: color.accent, label: "Vision updated", cat: "frontier" },
  "worker.observation": { icon: "MessageSquare", color: color.dim, label: "Worker observation", cat: "task" },
  "loop.halted": { icon: "OctagonAlert", color: color.red, label: "Loop halted", cat: "loop" },
};

export const SEVERITY_META: Record<Severity, Meta> = {
  sev1: { icon: "OctagonAlert", color: color.red, label: "Sev 1" },
  sev2: { icon: "TriangleAlert", color: color.amber, label: "Sev 2" },
  sev3: { icon: "Info", color: color.blue, label: "Sev 3" },
};

export const SAGA_META: Record<SagaState, Meta & { pulse: boolean }> = {
  DISPATCHED: { icon: "Send", color: color.slate, label: "Dispatched", pulse: false },
  RUNNING: { icon: "LoaderCircle", color: color.accent, label: "Running", pulse: true },
  AWAITING_CRITIC: { icon: "ScanSearch", color: color.violet, label: "Awaiting critic", pulse: true },
  REVISING: { icon: "RefreshCw", color: color.amber, label: "Revising", pulse: true },
  MERGED: { icon: "GitMerge", color: color.emerald, label: "Merged", pulse: false },
  ABANDONED: { icon: "CircleSlash", color: color.dim, label: "Abandoned", pulse: false },
  COMPENSATED: { icon: "Undo2", color: color.amber, label: "Compensated", pulse: false },
  QUARANTINED: { icon: "ShieldAlert", color: color.red, label: "Quarantined", pulse: false },
};

export const PR_LABEL_META: Record<PrLabel, Meta> = {
  "critic:blocking": { icon: "Ban", color: color.red, label: "critic:blocking" },
  "critic:suspicious": { icon: "Eye", color: color.amber, label: "critic:suspicious" },
  "loop:auto-rescued": { icon: "LifeBuoy", color: color.accent, label: "loop:auto-rescued" },
  "loop:ready": { icon: "CircleDot", color: color.emerald, label: "loop:ready" },
  clean: { icon: "CircleCheck", color: color.emerald, label: "clean" },
  epic: { icon: "Layers", color: color.violet, label: "epic" },
};

export const AXIS_META: Record<ValueAxis, { color: string; label: string; short: string }> = {
  "durable-control-plane": { color: color.accent, label: "Durable control plane", short: "Control plane" },
  "project-cognition-memory": { color: color.violet, label: "Project cognition & memory", short: "Cognition" },
  "frontier-generation": { color: color.blue, label: "Frontier generation", short: "Frontier" },
  "sandboxed-worker-execution": { color: color.emerald, label: "Sandboxed worker execution", short: "Sandbox" },
  "self-dogfood-operability": { color: color.amber, label: "Self-dogfood operability", short: "Operability" },
  "quality-and-evidence-gates": { color: color.red, label: "Quality & evidence gates", short: "Quality" },
};

export const MEMORY_META: Record<MemoryKind, Meta> = {
  episodic: { icon: "BookMarked", color: color.blue, label: "Episodic" },
  procedural: { icon: "Workflow", color: color.emerald, label: "Procedural" },
  rejected_path: { icon: "SignpostBig", color: color.amber, label: "Rejected path" },
};

export const VERDICT_META: Record<CriticVerdict, Meta> = {
  approved: { icon: "CircleCheck", color: color.emerald, label: "Approved" },
  changes_requested: { icon: "MessageSquareWarning", color: color.amber, label: "Changes requested" },
  error: { icon: "CircleX", color: color.red, label: "Critic error" },
};

// Extended palette. `C` is the canonical object the screens/components reference; it adds the
// text*/hairline/*Dim aliases the design spec uses so ported code reads 1:1. Prefer `C` in
// components; `color` remains for existing scaffold code.
export const C = {
  ...color,
  textHi: color.hi,
  textMid: color.mid,
  textDim: color.dim,
  textFaint: color.faint,
  hairline: "rgba(255,255,255,0.06)",
  hairline2: "rgba(255,255,255,0.10)",
  accentDim: "#0e7490",
  accentDeep: "#155e75",
  emeraldDim: "#0f5132",
  amberDim: "#5c4514",
  redDim: "#5c1a1a",
  violetDim: "#3b2d63",
  blueDim: "#1e3a5f",
} as const;

// Accessors with safe fallbacks (mirror the prototype's window.* helpers).
export const eventMeta = (kind: string): Meta & { cat: string } =>
  EVENT_META[kind as EventKind] ?? { icon: "Circle", color: color.dim, label: kind, cat: "other" };
export const sevMeta = (s: string): Meta => SEVERITY_META[s as Severity] ?? SEVERITY_META.sev3;
export const sagaMeta = (s: string): Meta & { pulse: boolean } =>
  SAGA_META[s as SagaState] ?? SAGA_META.DISPATCHED;
export const labelMeta = (l: string): Meta =>
  PR_LABEL_META[l as PrLabel] ?? { icon: "Tag", color: color.dim, label: l };
export const axisMeta = (a: string): { color: string; label: string; short: string } =>
  AXIS_META[a as ValueAxis] ?? { color: color.dim, label: a, short: a };
export const memoryMeta = (k: string): Meta => MEMORY_META[k as MemoryKind] ?? MEMORY_META.episodic;
export const verdictMeta = (v: string): Meta => VERDICT_META[v as CriticVerdict] ?? VERDICT_META.error;
