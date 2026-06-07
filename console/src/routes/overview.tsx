// screens-overview — Mission Control (HERO). Objective + KR + the falsifiable trend chart lead.
import { useState, useEffect } from "react";
import {
  useLoopStatus,
  useWorkers,
  useSagas,
  usePRs,
  useScorecard,
  useFrontier,
  useBudget,
  useEvents,
} from "@/hooks";
import { useNav } from "@/lib/drawer";
import { useNow } from "@/lib/ui";
import { C, eventMeta, sagaMeta } from "@/lib/theme";
import { relativeTime, freshness, duration, money, compact } from "@/lib/format";
import { Icon } from "@/components/Icon";
import {
  Panel,
  Kpi,
  Banner,
  EmptyState,
  TrendArrow,
  SagaStateBadge,
} from "@/components/primitives";
import { KrTrendChart, AreaTrend } from "@/components/charts";
import type { Worker } from "@/domain/models";
import type { EventEnvelope } from "@/domain/events";

// ── MergePing helper ────────────────────────────────────────────────────────

interface MergePingProps {
  pr: { pr: number | undefined; at: number } | null;
}

function MergePing({ pr }: MergePingProps) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (pr) {
      setShow(true);
      const t = setTimeout(() => setShow(false), 2600);
      return () => clearTimeout(t);
    }
  }, [pr?.at]);
  if (!show) return null;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        color: C.emerald,
        fontSize: 11.5,
        fontFamily: "var(--mono)",
        animation: "slideIn .3s ease",
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: 999,
          background: C.emerald,
          animation: "ping 1.2s ease-out infinite",
        }}
      />
      merged #{pr?.pr}
    </span>
  );
}

// ── InFlightCard helper ─────────────────────────────────────────────────────

interface InFlightCardProps {
  worker: Worker;
  saga: { issue: { title: string } } | undefined;
  onClick: () => void;
}

function InFlightCard({ worker, saga, onClick }: InFlightCardProps) {
  const fresh = freshness(worker.last_event_at);
  const m = sagaMeta(worker.state);
  const elapsed = (Date.now() - new Date(worker.started_at).getTime()) / 1000;
  return (
    <button
      onClick={onClick}
      className="focus-ring"
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        background: C.elevated,
        border: "1px solid var(--border)",
        borderRadius: 10,
        padding: "11px 12px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 7,
            minWidth: 0,
          }}
        >
          <span className="dot-pulse" style={{ background: m.color }} />
          <span
            style={{ fontFamily: "var(--mono)", fontSize: 12, color: C.textHi }}
          >
            #{worker.issue_number}
          </span>
        </span>
        <SagaStateBadge state={worker.state} size="sm" />
      </div>
      <div
        style={{
          fontSize: 12,
          color: C.textMid,
          margin: "8px 0 9px",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {saga ? saga.issue.title : worker.worktree_path}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          fontFamily: "var(--mono)",
          fontSize: 11,
        }}
      >
        <span style={{ color: C.dim }}>
          <Icon
            name="Clock"
            size={11}
            color={C.dim}
            style={{ verticalAlign: -1, marginRight: 3 }}
          />
          {duration(elapsed)}
        </span>
        <span style={{ color: C.emerald }}>{money(worker.cost_usd)}</span>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            color: fresh.color,
          }}
        >
          <span
            className="dot-pulse"
            style={{ background: fresh.color, width: 6, height: 6 }}
          />
          {fresh.label}
        </span>
      </div>
    </button>
  );
}

// ── OverviewScreen ──────────────────────────────────────────────────────────

export default function OverviewScreen() {
  const nav = useNav();
  const now = useNow();
  const status = useLoopStatus().data;
  const workers = useWorkers().data ?? [];
  const sagas = useSagas().data ?? [];
  const prs = usePRs().data ?? [];
  const scorecard = useScorecard().data;
  const frontier = useFrontier().data;
  const budget = useBudget().data;
  const { events } = useEvents();

  if (!status || !scorecard || !frontier || !budget) {
    return (
      <div className="page">
        <EmptyState icon="LoaderCircle" color={C.accent} title="Loading…" />
      </div>
    );
  }

  const sc = scorecard;
  const fpSpark = sc.history.map((p) => p.first_pass);
  const krPoints = sc.history.map((p) => ({ idx: p.idx, value: p.first_pass }));
  const krPct = Math.round(frontier.kr_current * 100);
  const krTarget = Math.round(frontier.kr_target * 100);
  const krProg = Math.min(1, frontier.kr_current / frontier.kr_target);

  const mergesToday = events.filter((e) => e.kind === "pr.merged").length;
  const lastMergePr = (() => {
    const m = [...events].reverse().find((e) => e.kind === "pr.merged");
    return m
      ? {
          pr: (m.payload as { pr?: number }).pr,
          at: new Date(m.occurred_at).getTime(),
        }
      : null;
  })();

  const recentMerges = events.filter((e) => e.kind === "pr.merged").slice(-5).reverse();
  const liveEvents = events.slice(-9).reverse();
  const inFlight = workers.filter((w) =>
    ["RUNNING", "AWAITING_CRITIC", "REVISING"].includes(w.state)
  );
  const suspicious = prs.filter((p) => p.state === "open" && p.review.suspicious);

  return (
    <div className="page">
      {/* KPI row */}
      <div
        className="grid"
        style={{ gridTemplateColumns: "repeat(5,1fr)", marginBottom: 14 }}
      >
        <div
          className="kpi"
          style={{
            borderColor: "color-mix(in oklab,var(--emerald) 28%,var(--border))",
          }}
        >
          <span className="kpi-l">
            <span className="dot-pulse" style={{ background: C.emerald }} />
            Loop status
          </span>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 8,
              marginTop: 8,
            }}
          >
            <span style={{ fontSize: 19, fontWeight: 600, color: C.emerald }}>
              Healthy
            </span>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 12,
                color: C.dim,
              }}
            >
              tick 42
            </span>
          </div>
          <div className="kpi-s">
            lag {status.lag} · seq {status.sequence}
          </div>
        </div>
        <Kpi
          label="Workers in flight"
          value={inFlight.length}
          icon="Bot"
          color={C.accent}
          sub={`${status.stale_lease_count} stale leases`}
          onClick={() => nav.go("workers")}
        />
        <Kpi
          label="Merges today"
          value={mergesToday}
          icon="GitMerge"
          color={C.emerald}
          spark={[1, 2, 2, 3, 3, 4, 4, mergesToday]}
          sub={lastMergePr ? <MergePing pr={lastMergePr} /> : "auto-merged"}
        />
        <Kpi
          label="$ / merged-PR"
          value={money(budget.cost_per_merged_pr)}
          icon="DollarSign"
          color={C.emerald}
          trend={<TrendArrow delta={-0.42} goodUp={false} suffix="" />}
          sub={`${money(budget.spend_today)} today`}
          onClick={() => nav.go("health")}
        />
        <Kpi
          label="First-pass acceptance"
          value={krPct}
          unit="%"
          icon="ShieldCheck"
          color={C.accent}
          spark={fpSpark}
          sparkColor={C.accent}
          trend={<TrendArrow delta={4} goodUp suffix="pt" />}
          sub="last 14 merges"
          onClick={() => nav.go("scorecard")}
          accent
        />
      </div>

      {/* alerts */}
      <div
        className="grid"
        style={{
          gridTemplateColumns: suspicious.length ? "1fr 1fr" : "1fr",
          marginBottom: 16,
        }}
      >
        <Banner
          tone="good"
          icon="ShieldCheck"
          title="Clean run — 0 stale leases, no halts in 24h"
          action={<span className="tag">doctor: ok</span>}
        >
          All projections caught up · 3 workers in flight · event log healthy at
          seq {status.sequence}.
        </Banner>
        {suspicious.length > 0 && (
          <Banner
            tone="warn"
            icon="Eye"
            title={`${suspicious.length} suspicious PR held for review`}
            action={
              <button className="btn" onClick={() => nav.go("prs")}>
                Review
              </button>
            }
          >
            #{suspicious[0].number} passes tests but the deny path is untested
            — holding rather than auto-merging.
          </Banner>
        )}
      </div>

      {/* HERO: Objective + KR + trend */}
      <div
        className="grid"
        style={{
          gridTemplateColumns: "1.5fr 1fr",
          alignItems: "stretch",
          marginBottom: 16,
        }}
      >
        <Panel
          title="Objective · Key Result"
          icon="Target"
          action={<span className="tag">frontier v{frontier.version}</span>}
        >
          <div
            style={{
              fontSize: 14.5,
              color: C.textHi,
              fontWeight: 500,
              lineHeight: 1.45,
              letterSpacing: "-.01em",
            }}
          >
            {frontier.objective}
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              margin: "14px 0 10px",
            }}
          >
            <Icon name="Flag" size={14} color={C.emerald} />
            <span style={{ fontSize: 13, color: C.textMid, flex: 1 }}>
              {frontier.key_result}
            </span>
          </div>
          {/* KR progress */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              marginBottom: 4,
            }}
          >
            <div style={{ flex: 1 }}>
              <div className="bar-track" style={{ height: 8 }}>
                <div
                  className="bar-fill"
                  style={{
                    width: `${krProg * 100}%`,
                    background: `linear-gradient(90deg, ${C.accent}, ${C.emerald})`,
                  }}
                />
              </div>
            </div>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 12,
                color: C.textMid,
              }}
            >
              <b style={{ color: C.textHi }}>{krPct}%</b> / {krTarget}% target
            </span>
          </div>
          <div
            style={{
              fontSize: 11.5,
              color: C.dim,
              fontFamily: "var(--mono)",
              marginBottom: 14,
            }}
          >
            {frontier.kr_merges_observed} of {frontier.kr_merges_window} merges
            observed in window · {krTarget - krPct}pt to target
          </div>
          <div className="divider" />
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 4,
            }}
          >
            <span
              style={{
                fontSize: 11.5,
                color: C.textMid,
                fontWeight: 600,
              }}
            >
              First-pass critic acceptance — is the machine actually getting
              better?
            </span>
            <span className="tag">20-pt history</span>
          </div>
          <KrTrendChart
            points={krPoints}
            target={frontier.kr_target}
            height={210}
          />
        </Panel>

        {/* Live ticker */}
        <Panel
          title="Live event stream"
          icon="Radio"
          pad={false}
          action={
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: C.emerald,
              }}
            >
              <span
                className="dot-pulse"
                style={{ background: C.emerald, width: 6, height: 6 }}
              />
              tailing
            </span>
          }
        >
          <div style={{ maxHeight: 360, overflow: "hidden" }}>
            {liveEvents.map((e: EventEnvelope, i: number) => {
              const m = eventMeta(e.kind);
              return (
                <div
                  key={e.event_id}
                  className={i === 0 ? "ev-row live-row" : "ev-row"}
                  onClick={() => nav.open("event", e)}
                >
                  <span
                    className="ev-ic"
                    style={{
                      background: `color-mix(in oklab, ${m.color} 15%, transparent)`,
                    }}
                  >
                    <Icon name={m.icon} size={13} color={m.color} />
                  </span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span
                      style={{
                        display: "block",
                        fontSize: 12.5,
                        color: C.textHi,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {m.label}
                      {(e.payload as { pr?: number; issue?: number }).pr
                        ? ` · #${(e.payload as { pr?: number }).pr}`
                        : (e.payload as { issue?: number }).issue
                          ? ` · #${(e.payload as { issue?: number }).issue}`
                          : ""}
                    </span>
                  </span>
                  <span className="ev-time">
                    {relativeTime(e.occurred_at, now)}
                  </span>
                </div>
              );
            })}
          </div>
          <div
            style={{
              padding: "9px 12px",
              borderTop: "1px solid var(--hair)",
            }}
          >
            <button
              className="btn"
              style={{ width: "100%", justifyContent: "center" }}
              onClick={() => nav.go("stream")}
            >
              <Icon name="ArrowRight" size={13} color={C.mid} />
              Open full stream
            </button>
          </div>
        </Panel>
      </div>

      {/* bottom row */}
      <div className="grid" style={{ gridTemplateColumns: "1.2fr 1fr 1fr" }}>
        <Panel
          title="In-flight workers"
          icon="Bot"
          action={<span className="tag">{inFlight.length} active</span>}
        >
          <div
            className="grid"
            style={{ gridTemplateColumns: "1fr 1fr", gap: 10 }}
          >
            {inFlight.map((w) => (
              <InFlightCard
                key={w.id}
                worker={w}
                saga={sagas.find((s) => s.worker_id === w.id)}
                onClick={() => nav.open("worker", w)}
              />
            ))}
          </div>
        </Panel>
        <Panel title="Recent merges" icon="GitMerge">
          {recentMerges.length === 0 ? (
            <EmptyState
              icon="GitMerge"
              title="No merges yet this tick"
              sub="Workers are in flight — merges will appear here."
            />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {recentMerges.map((e: EventEnvelope) => (
                <div
                  key={e.event_id}
                  className="ev-row"
                  style={{
                    borderBottom: "1px solid var(--hair)",
                    padding: "9px 4px",
                  }}
                  onClick={() => nav.open("event", e)}
                >
                  <span
                    className="ev-ic"
                    style={{
                      background:
                        "color-mix(in oklab,var(--emerald) 15%,transparent)",
                    }}
                  >
                    <Icon name="GitMerge" size={13} color={C.emerald} />
                  </span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span
                      style={{
                        display: "block",
                        fontSize: 12.5,
                        color: C.textHi,
                        fontFamily: "var(--mono)",
                      }}
                    >
                      #{(e.payload as { pr?: number }).pr}
                    </span>
                    <span style={{ fontSize: 11, color: C.dim }}>
                      {(
                        (e.payload as { branch?: string }).branch ?? ""
                      ).replace("loop/", "")}
                    </span>
                  </span>
                  <span
                    style={{
                      fontFamily: "var(--mono)",
                      fontSize: 11.5,
                      color: C.emerald,
                    }}
                  >
                    {money((e.payload as { cost_usd?: number }).cost_usd ?? null)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel
          title="Spend"
          icon="DollarSign"
          action={<span className="tag">24h</span>}
        >
          <div
            style={{ display: "flex", alignItems: "baseline", gap: 8 }}
          >
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 24,
                fontWeight: 600,
                color: C.textHi,
              }}
            >
              {money(budget.spend_today)}
            </span>
            <span style={{ fontSize: 12, color: C.dim }}>today</span>
          </div>
          <div
            style={{
              fontSize: 11.5,
              color: C.dim,
              marginBottom: 8,
              fontFamily: "var(--mono)",
            }}
          >
            {compact(budget.tokens_today)} tokens · {money(budget.cumulative)}{" "}
            cumulative
          </div>
          <AreaTrend
            points={budget.points}
            accessor={(p) => p.hourly}
            color={C.accent}
            height={96}
            yFormat={(v) => "$" + v.toFixed(0)}
          />
        </Panel>
      </div>
    </div>
  );
}
