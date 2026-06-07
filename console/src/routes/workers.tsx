// src/routes/workers.tsx — WorkersScreen ported from prototype/screens-workers.jsx
import { useMemo } from "react";
import { useWorkers, useSagas, useLoopStatus } from "@/hooks";
import { useNav } from "@/lib/drawer";
import { useNow } from "@/lib/ui";
import { C } from "@/lib/theme";
import { money, compact, duration, freshness } from "@/lib/format";
import { Panel, Kpi, Banner, DataTable, SagaStateBadge } from "@/components/primitives";
import { Icon } from "@/components/Icon";
import type { ColumnDef } from "@/components/primitives";
import type { Worker } from "@/domain/models";

export default function WorkersScreen() {
  const nav = useNav();
  const workers = useWorkers().data ?? [];
  const sagas = useSagas().data ?? [];
  const status = useLoopStatus().data;
  const now = useNow();

  const active = useMemo(
    () => workers.filter((w) => ["RUNNING", "AWAITING_CRITIC", "REVISING"].includes(w.state)),
    [workers],
  );
  const totalCost = useMemo(() => workers.reduce((s, w) => s + w.cost_usd, 0), [workers]);
  const totalTokens = useMemo(() => workers.reduce((s, w) => s + w.tokens, 0), [workers]);
  const staleLease = status?.stale_lease_count ?? 0;

  const columns = useMemo<ColumnDef<Worker>[]>(
    () => [
      {
        id: "id",
        header: "Worker",
        width: 130,
        sortVal: (w) => w.id,
        cell: (w) => (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <span
              className="ev-ic"
              style={{
                width: 24,
                height: 24,
                background: "color-mix(in oklab,var(--accent) 14%,transparent)",
              }}
            >
              <Icon name="Bot" size={13} color={C.accent} />
            </span>
            <span style={{ fontFamily: "var(--mono)", color: C.textHi }}>{w.id}</span>
          </span>
        ),
      },
      {
        id: "issue",
        header: "Issue",
        sortVal: (w) => w.issue_number,
        cell: (w) => {
          const s = sagas.find((x) => x.worker_id === w.id);
          return (
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: "var(--mono)", color: C.accent, fontSize: 12 }}>
                #{w.issue_number}
              </div>
              <div
                style={{
                  fontSize: 11.5,
                  color: C.dim,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  maxWidth: 260,
                }}
              >
                {s ? s.issue.title : w.worktree_path}
              </div>
            </div>
          );
        },
      },
      {
        id: "state",
        header: "State",
        sortVal: (w) => w.state,
        cell: (w) => <SagaStateBadge state={w.state} size="sm" />,
      },
      {
        id: "elapsed",
        header: "Elapsed",
        align: "right",
        sortVal: (w) => now - new Date(w.started_at).getTime(),
        cell: (w) => (
          <span style={{ fontFamily: "var(--mono)", color: C.textMid }}>
            {duration((now - new Date(w.started_at).getTime()) / 1000)}
          </span>
        ),
      },
      {
        id: "cost",
        header: "Cost",
        align: "right",
        sortVal: (w) => w.cost_usd,
        cell: (w) => (
          <span style={{ fontFamily: "var(--mono)", color: C.emerald }}>{money(w.cost_usd)}</span>
        ),
      },
      {
        id: "tokens",
        header: "Tokens",
        align: "right",
        sortVal: (w) => w.tokens,
        cell: (w) => (
          <span style={{ fontFamily: "var(--mono)", color: C.textMid }}>{compact(w.tokens)}</span>
        ),
      },
      {
        id: "hb",
        header: "Heartbeat",
        align: "right",
        sortVal: (w) => new Date(w.last_event_at).getTime(),
        cell: (w) => {
          const f = freshness(w.last_event_at, now);
          return (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                justifyContent: "flex-end",
              }}
            >
              <span className="dot-pulse" style={{ background: f.color, width: 6, height: 6 }} />
              <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: f.color }}>
                {f.label}
              </span>
            </span>
          );
        },
      },
      {
        id: "go",
        header: "",
        width: 30,
        cell: () => <Icon name="ChevronRight" size={14} color={C.faint} />,
      },
    ],
    [sagas, now],
  );

  return (
    <div className="page">
      <div className="grid" style={{ gridTemplateColumns: "repeat(4,1fr)", marginBottom: 16 }}>
        <Kpi
          label="Active workers"
          value={active.length}
          icon="Bot"
          color={C.accent}
          sub={`${workers.length} total dispatched`}
        />
        <Kpi
          label="Stale leases"
          value={staleLease}
          icon="TimerOff"
          color={staleLease ? C.amber : C.emerald}
          sub={staleLease ? "needs recovery" : "all leases fresh"}
        />
        <Kpi
          label="In-flight spend"
          value={money(totalCost)}
          icon="DollarSign"
          color={C.emerald}
          sub="across active workers"
        />
        <Kpi
          label="Tokens"
          value={compact(totalTokens)}
          icon="Hash"
          color={C.blue}
          sub="this generation"
        />
      </div>

      {staleLease === 0 && (
        <div style={{ marginBottom: 16 }}>
          <Banner tone="good" icon="ShieldCheck" title="0 stale leases">
            Every worker has heartbeat within its 90s lease. Crash-recovery has nothing to do — a
            quiet console is a healthy one.
          </Banner>
        </div>
      )}

      <Panel
        title="Workers"
        icon="Bot"
        pad={false}
        action={<span className="tag">{workers.length}</span>}
      >
        <DataTable<Worker>
          columns={columns}
          rows={workers}
          rowKey={(w) => w.id}
          onRow={(w) => nav.open("worker", w)}
          initialSort={{ id: "hb", dir: "desc" }}
        />
      </Panel>

      <div style={{ marginTop: 14 }}>
        <Panel
          title="Capability posture"
          icon="ShieldCheck"
          action={<span className="tag">least-privilege</span>}
        >
          <div
            style={{
              fontSize: 12.5,
              color: C.textMid,
              marginBottom: 12,
              lineHeight: 1.5,
            }}
          >
            Each worker is granted only the secrets, MCP servers, and egress its axis needs.
            Withheld capabilities are denied by default — the sandboxing story, made tangible.
          </div>
          <div className="grid" style={{ gridTemplateColumns: "repeat(3,1fr)" }}>
            {active.map((w) => (
              <button
                key={w.id}
                className="focus-ring"
                onClick={() => nav.open("worker", w)}
                style={{
                  textAlign: "left",
                  background: C.elevated,
                  border: "1px solid var(--border)",
                  borderRadius: 10,
                  padding: "12px 13px",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 9,
                  }}
                >
                  <span style={{ fontFamily: "var(--mono)", fontSize: 12, color: C.textHi }}>
                    {w.id}
                  </span>
                  <span className="tag">#{w.issue_number}</span>
                </div>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    fontSize: 11.5,
                    fontFamily: "var(--mono)",
                  }}
                >
                  <span
                    style={{
                      color: C.emerald,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
                    <Icon name="Check" size={12} color={C.emerald} />
                    {w.capability_policy.secret_names.length + w.capability_policy.mcp.length}{" "}
                    granted
                  </span>
                  <span
                    style={{
                      color: C.dim,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                    }}
                  >
                    <Icon name="Lock" size={11} color={C.faint} />
                    {w.withheld_secrets.length} withheld
                  </span>
                  <span
                    style={{
                      color: w.capability_policy.network_egress.length ? C.blue : C.dim,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      marginLeft: "auto",
                    }}
                  >
                    <Icon
                      name="Globe"
                      size={11}
                      color={w.capability_policy.network_egress.length ? C.blue : C.faint}
                    />
                    {w.capability_policy.network_egress.length
                      ? `${w.capability_policy.network_egress.length} egress`
                      : "deny"}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
