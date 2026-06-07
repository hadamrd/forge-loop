// screens-stream — full-height live feed: filter by kind/category/saga, tail toggle, click → drawer.
import { useState, useMemo, useEffect, useRef } from "react";
import { useLoopStatus, useEvents } from "@/hooks";
import { useNav } from "@/lib/drawer";
import { useNow, useUi } from "@/lib/ui";
import { C, eventMeta, EVENT_META } from "@/lib/theme";
import { relativeTime, money } from "@/lib/format";
import { Icon } from "@/components/Icon";
import { Panel, EmptyState, Pill, Tabs } from "@/components/primitives";
import type { EventEnvelope } from "@/domain/events";

// ── summarize helper ────────────────────────────────────────────────────────

function summarize(e: EventEnvelope): string {
  const p = (e.payload ?? {}) as Record<string, unknown>;
  switch (e.kind) {
    case "tick.started":
      return `Tick ${p.tick as number} started`;
    case "tick.completed":
      return `Tick ${p.tick as number} completed · ${(p.merged as number | undefined) ?? 0} merged`;
    case "task.planned":
      return `#${p.issue as number} ${(p.title as string | undefined) ?? ""}`;
    case "task.dispatched":
      return `#${p.issue as number} → ${p.worker as string} in ${p.worktree as string}`;
    case "task.heartbeat":
      return `cost ${money(p.cost_usd as number)}`;
    case "pr.opened":
      return `#${p.pr as number} ${(p.title as string | undefined) ?? ""} (+${(p.additions as number | undefined) ?? "?"} −${(p.deletions as number | undefined) ?? "?"})`;
    case "pr.merged":
      return `#${p.pr as number} merged from ${p.branch as string} · ${money(p.cost_usd as number)}`;
    case "merge.blocked":
      return (p.reason as string | undefined) ?? `#${p.pr as number} blocked`;
    case "critique.issued":
      return `#${p.pr as number} round ${p.round as number} · ${p.verdict as string} · ${(p.sev2 as number | undefined) ?? 0} sev2`;
    case "decision.made":
      return p.decision as string;
    case "memory.promoted":
      return `${p.memory as string}: ${p.title as string}`;
    case "memory.superseded":
      return `${p.old as string} → ${p.by as string}`;
    case "frontier.advanced":
      return `v${p.version as number} · ${(p.next_expansion as string | undefined) ?? ""}`;
    case "idea.rejected":
      return `${p.idea as string} — ${p.reason as string}`;
    case "vision.updated":
      return `v${p.version as number} · ${(p.note as string | undefined) ?? ""}`;
    case "compaction.performed":
      return p.freed as string;
    case "worker.observation":
      return p.note as string;
    case "worktree.reaped":
      return p.worktree as string;
    case "task.completed":
      return `#${p.issue as number} completed`;
    case "task.failed":
      return (p.reason as string | undefined) ?? `#${p.issue as number} failed`;
    case "task.compensated":
      return (p.reason as string | undefined) ?? `#${p.issue as number} compensated`;
    case "loop.halted":
      return (p.reason as string | undefined) ?? "loop halted";
    default:
      return JSON.stringify(p).slice(0, 80);
  }
}

// ── StreamScreen ────────────────────────────────────────────────────────────

export default function StreamScreen() {
  const nav = useNav();
  const now = useNow();
  const status = useLoopStatus().data;
  const { paused } = useUi();
  const { events } = useEvents();
  const [cat, setCat] = useState("all");
  const [kindFilter, setKindFilter] = useState<string | null>(null);
  const [tail, setTail] = useState(true);
  const [q, setQ] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const cats = [
    { id: "all", label: "All", icon: "List" },
    { id: "loop", label: "Loop", icon: "RefreshCw" },
    { id: "task", label: "Tasks", icon: "Send" },
    { id: "pr", label: "PRs", icon: "GitPullRequest" },
    { id: "critic", label: "Critic", icon: "ScanSearch" },
    { id: "frontier", label: "Frontier", icon: "Telescope" },
    { id: "memory", label: "Memory", icon: "BrainCircuit" },
    { id: "infra", label: "Infra", icon: "Server" },
  ];

  const filtered = useMemo(() => {
    let list = events;
    if (cat !== "all") list = list.filter((e) => eventMeta(e.kind).cat === cat);
    if (kindFilter) list = list.filter((e) => e.kind === kindFilter);
    if (q.trim()) {
      const s = q.toLowerCase();
      list = list.filter(
        (e) =>
          e.kind.includes(s) ||
          (e.saga_id ?? "").includes(s) ||
          JSON.stringify(e.payload).toLowerCase().includes(s)
      );
    }
    return [...list].reverse();
  }, [events, cat, kindFilter, q]);

  useEffect(() => {
    if (tail && scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [events, tail]);

  // counts per kind for the legend rail
  const kindCounts = useMemo(() => {
    const m: Record<string, number> = {};
    events.forEach((e) => {
      m[e.kind] = (m[e.kind] ?? 0) + 1;
    });
    return m;
  }, [events]);

  const activeKinds = Object.keys(EVENT_META).filter((k) => kindCounts[k]);

  return (
    <div
      className="page page-wide"
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 248px",
        gap: 16,
        alignItems: "start",
      }}
    >
      <div>
        {/* controls */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: 12,
            flexWrap: "wrap",
          }}
        >
          <Tabs
            tabs={cats}
            value={cat}
            onChange={(v) => {
              setCat(v);
              setKindFilter(null);
            }}
          />
          <div style={{ position: "relative", marginLeft: "auto" }}>
            <Icon
              name="Search"
              size={14}
              color={C.dim}
              style={{ position: "absolute", left: 10, top: 9 }}
            />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="filter payload, saga, kind…"
              className="focus-ring"
              style={{
                height: 32,
                width: 220,
                paddingLeft: 30,
                paddingRight: 10,
                background: C.elevated,
                border: "1px solid var(--border)",
                borderRadius: 8,
                color: C.textHi,
                fontSize: 12.5,
                fontFamily: "var(--mono)",
              }}
            />
          </div>
          <button
            className="btn"
            onClick={() => setTail(!tail)}
            style={
              tail
                ? {
                    borderColor:
                      "color-mix(in oklab,var(--emerald) 32%,transparent)",
                    color: C.emerald,
                  }
                : undefined
            }
          >
            <Icon
              name={tail ? "ArrowDownToLine" : "Pause"}
              size={13}
              color={tail ? C.emerald : C.mid}
            />
            {tail ? "Tailing" : "Paused scroll"}
          </button>
        </div>

        {kindFilter && (
          <div style={{ marginBottom: 10 }}>
            <Pill icon={eventMeta(kindFilter).icon} color={eventMeta(kindFilter).color}>
              {eventMeta(kindFilter).label}
              <span
                onClick={() => setKindFilter(null)}
                style={{ cursor: "pointer", marginLeft: 4, display: "inline-flex" }}
              >
                <Icon name="X" size={12} color={eventMeta(kindFilter).color} />
              </span>
            </Pill>
          </div>
        )}

        <Panel
          pad={false}
          title={`${filtered.length} events`}
          icon="Radio"
          action={
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: paused ? C.amber : C.emerald,
              }}
            >
              <span
                className="dot-pulse"
                style={{
                  background: paused ? C.amber : C.emerald,
                  width: 6,
                  height: 6,
                }}
              />
              {paused
                ? "feed paused"
                : "live · seq " + (status?.sequence ?? "—")}
            </span>
          }
        >
          <div
            ref={scrollRef}
            className="scroll-y"
            style={{ maxHeight: "calc(100vh - 190px)" }}
          >
            {filtered.length === 0 ? (
              <EmptyState
                icon="Inbox"
                color={C.dim}
                title="No events match"
                sub="Loosen the filter to see the stream."
              />
            ) : (
              filtered.map((e: EventEnvelope, i: number) => {
                const m = eventMeta(e.kind);
                const isNew =
                  tail && i === 0 && e.sequence === (status?.sequence ?? -1);
                return (
                  <div
                    key={e.event_id}
                    className={isNew ? "ev-row live-row" : "ev-row"}
                    onClick={() => nav.open("event", e)}
                  >
                    <span className="ev-seq">{e.sequence}</span>
                    <span
                      className="ev-ic"
                      style={{
                        background: `color-mix(in oklab, ${m.color} 15%, transparent)`,
                      }}
                    >
                      <Icon name={m.icon} size={13} color={m.color} />
                    </span>
                    <span
                      style={{
                        width: 132,
                        flexShrink: 0,
                        fontSize: 12.5,
                        color: m.color,
                        fontFamily: "var(--mono)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {e.kind}
                    </span>
                    <span
                      style={{
                        flex: 1,
                        minWidth: 0,
                        fontSize: 12.5,
                        color: C.textMid,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {summarize(e)}
                    </span>
                    {e.saga_id && (
                      <span className="tag" style={{ flexShrink: 0 }}>
                        {e.saga_id}
                      </span>
                    )}
                    {e.causal_event_id && (
                      <Icon name="Link2" size={12} color={C.faint} />
                    )}
                    <span
                      className="ev-time"
                      style={{ width: 64, textAlign: "right" }}
                    >
                      {relativeTime(e.occurred_at, now)}
                    </span>
                  </div>
                );
              })
            )}
          </div>
        </Panel>
      </div>

      {/* legend / kind rail */}
      <div
        style={{
          position: "sticky",
          top: 0,
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        <Panel title="Event kinds" icon="Filter" dense>
          <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
            {activeKinds.map((k) => {
              const m = eventMeta(k);
              const on = kindFilter === k;
              return (
                <button
                  key={k}
                  onClick={() => setKindFilter(on ? null : k)}
                  className="focus-ring"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "5px 7px",
                    borderRadius: 6,
                    border: "none",
                    background: on
                      ? `color-mix(in oklab, ${m.color} 14%, transparent)`
                      : "transparent",
                    textAlign: "left",
                    width: "100%",
                  }}
                >
                  <Icon name={m.icon} size={13} color={m.color} />
                  <span
                    style={{
                      flex: 1,
                      fontSize: 11.5,
                      color: on ? C.textHi : C.textMid,
                      fontFamily: "var(--mono)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {k}
                  </span>
                  <span
                    style={{
                      fontSize: 10.5,
                      color: C.dim,
                      fontFamily: "var(--mono)",
                    }}
                  >
                    {kindCounts[k]}
                  </span>
                </button>
              );
            })}
          </div>
        </Panel>
        <Panel title="Causality" icon="Link2" dense>
          <div
            style={{ fontSize: 12, color: C.textMid, lineHeight: 1.55 }}
          >
            Rows with{" "}
            <Icon
              name="Link2"
              size={12}
              color={C.faint}
              style={{ verticalAlign: -2 }}
            />{" "}
            carry a <span className="code">causal_event_id</span>. Open any
            event to walk the chain back to the tick that started it.
          </div>
        </Panel>
      </div>
    </div>
  );
}
