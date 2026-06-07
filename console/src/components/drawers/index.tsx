// drawers/index.tsx — right-side detail drawer bodies.
// EventDrawerBody · WorkerDrawerBody · PrDrawerBody · SagaDrawerBody
// Local helpers PayloadView · CausalNode · CapRow are NOT exported.

import { Fragment } from "react";
import { Icon } from "@/components/Icon";
import { C, eventMeta, sevMeta, sagaMeta, verdictMeta } from "@/lib/theme";
import { relativeTime, clockTime, money, compact, freshness } from "@/lib/format";
import {
  Kpi,
  Banner,
  EmptyState,
  Pill,
  SagaStateBadge,
  SevBadge,
  LabelChip,
  AxisChip,
  SectionLabel,
  KV,
  DrawerHeader,
} from "@/components/primitives";
import { StepTrajectory } from "@/components/charts";
import { useNav } from "@/lib/drawer";
import { useEvents, useSagas, usePRs, useKillWorker } from "@/hooks";
import type { EventEnvelope } from "@/domain/events";
import type { Worker, PullRequest, Saga } from "@/domain/models";

// ── Local helpers (not exported) ─────────────────────────────────────────────

function PayloadView({ payload }: { payload: Record<string, unknown> | undefined }) {
  const entries = Object.entries(payload ?? {});
  if (!entries.length)
    return (
      <div style={{ color: C.dim, fontSize: 12, fontFamily: "var(--mono)" }}>{"{}"}</div>
    );
  return (
    <div
      style={{
        background: C.bg,
        border: "1px solid var(--hair)",
        borderRadius: 8,
        padding: "10px 12px",
        fontFamily: "var(--mono)",
        fontSize: 12,
        lineHeight: 1.7,
      }}
    >
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: "flex", gap: 10 }}>
          <span style={{ color: C.violet, flexShrink: 0 }}>{k}</span>
          <span style={{ color: C.textHi, wordBreak: "break-word" }}>
            {typeof v === "object" ? JSON.stringify(v) : String(v)}
          </span>
        </div>
      ))}
    </div>
  );
}

function CausalNode({
  ev,
  role,
  onClick,
  active,
}: {
  ev: EventEnvelope;
  role: string;
  onClick: () => void;
  active: boolean;
}) {
  const m = eventMeta(ev.kind);
  return (
    <button
      onClick={onClick}
      className="focus-ring"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        width: "100%",
        textAlign: "left",
        background: active
          ? "color-mix(in oklab,var(--accent) 10%,transparent)"
          : C.elevated,
        border:
          "1px solid " +
          (active
            ? "color-mix(in oklab,var(--accent) 32%,transparent)"
            : C.border),
        borderRadius: 9,
        padding: "8px 11px",
      }}
    >
      <span
        className="ev-ic"
        style={{ background: `color-mix(in oklab, ${m.color} 15%, transparent)` }}
      >
        <Icon name={m.icon} size={14} color={m.color} />
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>
        <span
          style={{ display: "block", fontSize: 12.5, color: C.textHi, fontWeight: 500 }}
        >
          {m.label}
        </span>
        <span
          style={{ display: "block", fontSize: 11, color: C.dim, fontFamily: "var(--mono)" }}
        >
          #{ev.sequence} · {role}
        </span>
      </span>
      <Icon name="ChevronRight" size={14} color={C.faint} />
    </button>
  );
}

function CapRow({
  icon,
  label,
  granted,
  withheld,
}: {
  icon: string;
  label: string;
  granted: string[];
  withheld: string[];
}) {
  return (
    <div style={{ padding: "10px 0", borderBottom: "1px solid var(--hair)" }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 7 }}
      >
        <Icon name={icon} size={13} color={C.mid} />
        <span style={{ fontSize: 12, color: C.textMid, fontWeight: 500 }}>{label}</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {granted.length === 0 && withheld.length === 0 && (
          <span style={{ fontSize: 11.5, color: C.dim }}>none</span>
        )}
        {granted.map((g) => (
          <span
            key={g}
            className="pill"
            style={{
              height: 22,
              fontSize: 11,
              color: C.emerald,
              paddingInline: 8,
              background: "color-mix(in oklab,var(--emerald) 12%,transparent)",
              borderColor: "color-mix(in oklab,var(--emerald) 26%,transparent)",
            }}
          >
            <Icon name="Check" size={11} color={C.emerald} />
            {g}
          </span>
        ))}
        {withheld.map((w) => (
          <span
            key={w}
            className="pill"
            style={{
              height: 22,
              fontSize: 11,
              color: C.dim,
              paddingInline: 8,
              background: "transparent",
              borderColor: C.border,
              textDecoration: "line-through",
              textDecorationColor: "color-mix(in oklab,var(--red) 50%,transparent)",
            }}
          >
            <Icon name="Lock" size={10} color={C.faint} />
            {w}
          </span>
        ))}
      </div>
    </div>
  );
}

// ── EventDrawerBody ───────────────────────────────────────────────────────────

export function EventDrawerBody({ event }: { event: EventEnvelope }) {
  const nav = useNav();
  const { events } = useEvents();
  const sagas = useSagas().data ?? [];
  const m = eventMeta(event.kind);

  // walk causal chain backward
  const chain: EventEnvelope[] = [];
  let cur: EventEnvelope | undefined = event;
  let guard = 0;
  while (cur && guard++ < 8) {
    chain.unshift(cur);
    const parentId: string | undefined = cur.causal_event_id;
    cur = parentId ? events.find((e) => e.event_id === parentId) : undefined;
  }
  const effects = events.filter((e) => e.causal_event_id === event.event_id);
  const saga = event.saga_id ? sagas.find((s) => s.saga_id === event.saga_id) : null;

  return (
    <>
      <DrawerHeader
        icon={m.icon}
        color={m.color}
        kicker={"event #" + event.sequence}
        title={m.label}
        onClose={nav.close}
        right={<span className="tag">{clockTime(event.occurred_at)}</span>}
      />
      <div className="drawer-body">
        <KV k="event_id" mono>
          {event.event_id}
        </KV>
        <KV k="kind" mono>
          {event.kind}
        </KV>
        <KV k="occurred_at" mono>
          {relativeTime(event.occurred_at)}
        </KV>
        {event.saga_id && (
          <KV k="saga_id" mono>
            <a
              onClick={() => saga && nav.open("saga", saga)}
              style={{ color: C.accent, cursor: saga ? "pointer" : "default" }}
            >
              {event.saga_id}
            </a>
          </KV>
        )}
        {event.task_id && (
          <KV k="task_id" mono>
            {event.task_id}
          </KV>
        )}

        <SectionLabel>Payload</SectionLabel>
        <PayloadView payload={event.payload as Record<string, unknown>} />

        <SectionLabel>Causal chain</SectionLabel>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {chain.length === 1 && (
            <div style={{ color: C.dim, fontSize: 12 }}>
              Root event — no upstream cause recorded.
            </div>
          )}
          {chain.map((e, i) => (
            <Fragment key={e.event_id}>
              <CausalNode
                ev={e}
                active={e.event_id === event.event_id}
                role={
                  i === chain.length - 1
                    ? "this event"
                    : i === 0
                    ? "origin"
                    : "caused next"
                }
                onClick={() =>
                  e.event_id !== event.event_id && nav.open("event", e)
                }
              />
              {i < chain.length - 1 && (
                <div
                  style={{
                    height: 12,
                    marginLeft: 24,
                    borderLeft: "2px solid var(--hair2)",
                  }}
                />
              )}
            </Fragment>
          ))}
        </div>

        {effects.length > 0 && (
          <>
            <SectionLabel>
              Downstream effects{" "}
              <span style={{ color: C.dim }}>{effects.length}</span>
            </SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {effects.slice(0, 6).map((e) => (
                <CausalNode
                  key={e.event_id}
                  ev={e}
                  role="effect"
                  onClick={() => nav.open("event", e)}
                  active={false}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </>
  );
}

// ── WorkerDrawerBody ──────────────────────────────────────────────────────────

export function WorkerDrawerBody({ worker }: { worker: Worker }) {
  const nav = useNav();
  const { mutate: killWorker } = useKillWorker();
  const fresh = freshness(worker.last_event_at);
  const cap = worker.capability_policy;
  const monologue = worker.monologue ?? [];

  return (
    <>
      <DrawerHeader
        icon="Bot"
        color={C.accent}
        kicker={worker.id}
        title={"Issue #" + worker.issue_number}
        onClose={nav.close}
        right={<SagaStateBadge state={worker.state} size="sm" />}
      />
      <div className="drawer-body">
        <div
          style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 4 }}
        >
          <Kpi label="Cost" value={money(worker.cost_usd)} icon="DollarSign" color={C.emerald} />
          <Kpi label="Tokens" value={compact(worker.tokens)} icon="Hash" color={C.blue} />
        </div>
        <KV k="model" mono>
          {worker.model}
        </KV>
        <KV k="worktree" mono>
          {worker.worktree_path}
        </KV>
        <KV k="started" mono>
          {relativeTime(worker.started_at)}
        </KV>
        <KV k="heartbeat">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span className="dot-pulse" style={{ background: fresh.color }} />
            <span style={{ color: fresh.color, fontFamily: "var(--mono)", fontSize: 12 }}>
              {fresh.label}
            </span>
            <span style={{ color: C.dim, fontSize: 11 }}>
              · {relativeTime(worker.last_event_at)}
            </span>
          </span>
        </KV>

        <SectionLabel>
          Capability policy{" "}
          <span style={{ color: C.dim, textTransform: "none", letterSpacing: 0 }}>
            least-privilege
          </span>
        </SectionLabel>
        <CapRow
          icon="KeyRound"
          label="Secrets"
          granted={cap.secret_names}
          withheld={worker.withheld_secrets}
        />
        <CapRow icon="Plug" label="MCP servers" granted={cap.mcp} withheld={[]} />
        <CapRow
          icon="Globe"
          label="Network egress"
          granted={cap.network_egress}
          withheld={
            cap.network_egress.length ? [] : ["* (default-deny)"]
          }
        />

        <SectionLabel
          right={<span className="tag">{worker.monologue_log_path}</span>}
        >
          Monologue
        </SectionLabel>
        <div
          className="mlog"
          style={{
            background: C.bg,
            border: "1px solid var(--hair)",
            borderRadius: 9,
            padding: "10px 12px",
            maxHeight: 280,
            overflowY: "auto",
          }}
        >
          {monologue.map((l, i) => {
            const lc =
              (
                {
                  plan: C.accent,
                  info: C.mid,
                  tool: C.violet,
                  warn: C.amber,
                  error: C.red,
                } as Record<string, string>
              )[l.level] ?? C.mid;
            return (
              <div className="mlog-line" key={i}>
                <span
                  style={{ color: lc, flexShrink: 0, width: 38, fontWeight: 600 }}
                >
                  {l.level}
                </span>
                <span style={{ color: C.textMid }}>{l.text}</span>
              </div>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            className="btn btn-danger"
            onClick={() => {
              killWorker(worker.id);
              nav.close();
            }}
          >
            <Icon name="Skull" size={14} color={C.red} />
            Kill worker
          </button>
          <button className="btn">
            <Icon name="ExternalLink" size={14} color={C.mid} />
            Open worktree
          </button>
        </div>
      </div>
    </>
  );
}

// ── PrDrawerBody ──────────────────────────────────────────────────────────────

export function PrDrawerBody({ pr }: { pr: PullRequest }) {
  const nav = useNav();
  const r = pr.review;
  const vMeta = verdictMeta(r.verdict);
  const sevTotal = r.sev_counts.sev1 + r.sev_counts.sev2 + r.sev_counts.sev3;

  return (
    <>
      <DrawerHeader
        icon="GitPullRequest"
        color={C.blue}
        kicker={"PR #" + pr.number}
        title={pr.title}
        onClose={nav.close}
      />
      <div className="drawer-body">
        <div
          style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}
        >
          {pr.labels.map((l) => (
            <LabelChip key={l} label={l} />
          ))}
          <span className="tag">
            <Icon
              name="GitBranch"
              size={11}
              color={C.dim}
              style={{ marginRight: 4, verticalAlign: -1 }}
            />
            {pr.branch}
          </span>
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            fontFamily: "var(--mono)",
            fontSize: 12.5,
            marginBottom: 14,
          }}
        >
          <span style={{ color: C.emerald }}>+{pr.additions}</span>
          <span style={{ color: C.red }}>−{pr.deletions}</span>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              color: pr.mergeable ? C.emerald : C.amber,
            }}
          >
            <Icon
              name={pr.mergeable ? "GitMerge" : "GitPullRequestClosed"}
              size={13}
              color={pr.mergeable ? C.emerald : C.amber}
            />
            {pr.mergeable ? "mergeable" : "blocked"}
          </span>
        </div>

        {r.suspicious && (
          <div style={{ marginBottom: 14 }}>
            <Banner tone="warn" icon="Eye" title="Suspicious — held for a human glance">
              {r.suspicious_reason}
            </Banner>
          </div>
        )}

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "12px 14px",
            borderRadius: 10,
            border:
              "1px solid " +
              `color-mix(in oklab, ${vMeta.color} 30%, transparent)`,
            background: `color-mix(in oklab, ${vMeta.color} 9%, transparent)`,
            marginBottom: 14,
          }}
        >
          <Icon name={vMeta.icon} size={18} color={vMeta.color} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600, color: C.textHi }}>
              {vMeta.label}
            </div>
            <div
              style={{
                fontSize: 11.5,
                color: C.dim,
                fontFamily: "var(--mono)",
              }}
            >
              critic round {r.round}
            </div>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {r.sev_counts.sev1 > 0 && (
              <SevBadge sev="sev1" count={r.sev_counts.sev1} />
            )}
            {r.sev_counts.sev2 > 0 && (
              <SevBadge sev="sev2" count={r.sev_counts.sev2} />
            )}
            {r.sev_counts.sev3 > 0 && (
              <SevBadge sev="sev3" count={r.sev_counts.sev3} />
            )}
            {sevTotal === 0 && (
              <Pill icon="Check" color={C.emerald} size="sm">
                clean
              </Pill>
            )}
          </div>
        </div>

        {r.findings.length > 0 ? (
          <>
            <SectionLabel>
              Findings <span style={{ color: C.dim }}>{r.findings.length}</span>
            </SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {r.findings.map((f, i) => {
                const sm = sevMeta(f.severity);
                return (
                  <div
                    key={i}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: 9,
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        padding: "7px 11px",
                        background: sm.color
                          ? `color-mix(in oklab, ${sm.color} 10%, transparent)`
                          : "transparent",
                        borderBottom: "1px solid var(--hair)",
                      }}
                    >
                      <Icon name={sm.icon} size={13} color={sm.color} />
                      <span
                        style={{
                          fontSize: 11.5,
                          fontWeight: 600,
                          color: sm.color,
                          fontFamily: "var(--mono)",
                        }}
                      >
                        {sm.label}
                      </span>
                      <span className="tag" style={{ marginLeft: "auto" }}>
                        {f.category}
                      </span>
                    </div>
                    <div style={{ padding: "9px 11px" }}>
                      <div
                        style={{
                          fontFamily: "var(--mono)",
                          fontSize: 11.5,
                          color: C.accent,
                          marginBottom: 5,
                        }}
                      >
                        {f.file}
                        <span style={{ color: C.dim }}>:{f.line}</span>
                      </div>
                      <div
                        style={{ fontSize: 12.5, color: C.textMid, lineHeight: 1.5 }}
                      >
                        {f.message}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        ) : (
          <EmptyState
            icon="ShieldCheck"
            title="No findings — clean run"
            sub="The critic raised nothing on this PR."
          />
        )}

        {r.minimal_path_to_green.length > 0 && (
          <>
            <SectionLabel>Minimal path to green</SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {r.minimal_path_to_green.map((s, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 9,
                    padding: "7px 10px",
                    borderRadius: 8,
                    background: C.elevated,
                  }}
                >
                  <Icon
                    name={s.done ? "CircleCheck" : "Circle"}
                    size={15}
                    color={s.done ? C.emerald : C.dim}
                  />
                  <span
                    style={{
                      fontSize: 12.5,
                      color: s.done ? C.dim : C.textHi,
                      textDecoration: s.done ? "line-through" : "none",
                    }}
                  >
                    {s.text}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}

        {r.history.length > 1 && (
          <>
            <SectionLabel>
              Repair-round trajectory{" "}
              <span style={{ color: C.dim, textTransform: "none", letterSpacing: 0 }}>
                sev2 per round
              </span>
            </SectionLabel>
            <div
              style={{
                background: C.bg,
                border: "1px solid var(--hair)",
                borderRadius: 9,
                padding: "10px 8px",
                display: "flex",
                justifyContent: "center",
              }}
            >
              <StepTrajectory history={r.history} width={420} height={96} />
            </div>
          </>
        )}
      </div>
    </>
  );
}

// ── SagaDrawerBody ────────────────────────────────────────────────────────────

export function SagaDrawerBody({ saga }: { saga: Saga }) {
  const nav = useNav();
  const { events } = useEvents();
  const prs = usePRs().data ?? [];
  const m = sagaMeta(saga.state);

  const sagaEvents = events
    .filter((e) => e.saga_id === saga.saga_id)
    .sort((a, b) => a.sequence - b.sequence);
  const pr = prs.find((p) => p.saga_id === saga.saga_id);

  return (
    <>
      <DrawerHeader
        icon={m.icon}
        color={m.color}
        kicker={saga.saga_id}
        title={"Issue #" + saga.issue.number}
        onClose={nav.close}
        right={<SagaStateBadge state={saga.state} size="sm" />}
      />
      <div className="drawer-body">
        <div style={{ fontSize: 13, color: C.textMid, marginBottom: 12 }}>
          {saga.issue.title}
        </div>
        <div style={{ marginBottom: 12 }}>
          <AxisChip axis={saga.issue.axis} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
          <Kpi
            label="Repair rounds"
            value={saga.repair_rounds}
            icon="RefreshCw"
            color={C.amber}
          />
          <Kpi
            label="Cost"
            value={money(saga.cost_usd)}
            icon="DollarSign"
            color={C.emerald}
          />
          <Kpi
            label="Branch"
            value={
              <span style={{ fontSize: 13 }}>
                {saga.branch.split("/")[1] ?? saga.branch}
              </span>
            }
            icon="GitBranch"
            color={C.blue}
          />
        </div>

        {pr && (
          <div style={{ marginTop: 14 }}>
            <button
              className="btn"
              style={{ width: "100%", justifyContent: "space-between" }}
              onClick={() => nav.open("pr", pr)}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <Icon name="GitPullRequest" size={14} color={C.blue} />
                PR #{pr.number} · critic review
              </span>
              <Icon name="ChevronRight" size={14} color={C.dim} />
            </button>
          </div>
        )}

        <SectionLabel>
          Event timeline{" "}
          <span style={{ color: C.dim }}>{sagaEvents.length}</span>
        </SectionLabel>
        <div style={{ position: "relative", paddingLeft: 8 }}>
          {sagaEvents.map((e, i) => {
            const em = eventMeta(e.kind);
            return (
              <div
                key={e.event_id}
                style={{
                  display: "flex",
                  gap: 11,
                  position: "relative",
                  paddingBottom: i < sagaEvents.length - 1 ? 12 : 0,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                  }}
                >
                  <span
                    className="ev-ic"
                    style={{
                      width: 24,
                      height: 24,
                      background: `color-mix(in oklab, ${em.color} 15%, transparent)`,
                    }}
                  >
                    <Icon name={em.icon} size={12} color={em.color} />
                  </span>
                  {i < sagaEvents.length - 1 && (
                    <span
                      style={{
                        flex: 1,
                        width: 2,
                        background: "var(--hair2)",
                        marginTop: 2,
                        minHeight: 14,
                      }}
                    />
                  )}
                </div>
                <button
                  onClick={() => nav.open("event", e)}
                  className="focus-ring"
                  style={{
                    flex: 1,
                    textAlign: "left",
                    background: "none",
                    border: "none",
                    padding: "2px 0 0",
                    minWidth: 0,
                  }}
                >
                  <div
                    style={{ fontSize: 12.5, color: C.textHi, fontWeight: 500 }}
                  >
                    {em.label}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: C.dim,
                      fontFamily: "var(--mono)",
                    }}
                  >
                    #{e.sequence} · {relativeTime(e.occurred_at)}
                  </div>
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
