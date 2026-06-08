import { useLoopStatus, usePipeline, useBudget } from "@/hooks";
import { Panel, Kpi, EmptyState } from "@/components/primitives";
import { AreaTrend } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { C } from "@/lib/theme";
import { money } from "@/lib/format";

export default function HealthScreen() {
  const status = useLoopStatus().data;
  const pipeline = usePipeline().data ?? [];
  const budget = useBudget().data;

  if (!status || !budget) {
    return (
      <div className="page">
        <EmptyState icon="LoaderCircle" color={C.accent} title="Loading…" />
      </div>
    );
  }

  const projections = status.projections ?? [];
  const maxLag = projections.length > 0 ? Math.max(...projections.map((p) => p.lag)) : 0;
  const entropy = status.operational_entropy;
  const oe = (v: number | null) => (v === null ? "—" : v);

  return (
    <div className="page page-wide">
      <div className="grid" style={{ gridTemplateColumns: "repeat(4,1fr)", marginBottom: 16 }}>
        <Kpi label="Boot doctor" value="ok" icon="Stethoscope" color={C.emerald} sub={status.boot.version} />
        <Kpi label="Event-log seq" value={status.sequence} icon="Hash" color={C.accent} sub={`${status.event_log.size_mb} MB · ${status.event_log.path}`} />
        <Kpi label="Max projection lag" value={maxLag} icon="Gauge" color={maxLag > 6 ? C.amber : C.emerald} sub={maxLag > 6 ? "memory rebuilding" : "caught up"} />
        <Kpi label="Cost / merged-PR" value={money(budget.cost_per_merged_pr)} icon="DollarSign" color={C.emerald} sub={`${money(budget.spend_today)} today`} />
      </div>

      {/* Issue #402 — operational-entropy: one read-only divergence row. */}
      <div className="grid" style={{ gridTemplateColumns: "repeat(4,1fr)", marginBottom: 16 }}>
        <Kpi label="Open branches" value={oe(entropy.open_branches)} icon="GitBranch" color={C.blue} sub="local branches" />
        <Kpi label="Live worktrees" value={oe(entropy.live_worktrees)} icon="FolderTree" color={C.blue} sub="git worktree list" />
        <Kpi label="Open epics" value={oe(entropy.open_epics)} icon="Layers" color={C.blue} sub="label:epic" />
        <Kpi label="Backlog age" value={oe(entropy.backlog_age_days)} unit="d" icon="Clock" color={(entropy.backlog_age_days ?? 0) > 14 ? C.amber : C.emerald} sub="oldest open issue" />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 14 }}>
        <Panel title="Projections" icon="Layers" action={<span className="tag">{projections.length}</span>}>
          <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            {projections.map((p) => (
              <div key={p.name} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <Icon name="Box" size={13} color={p.lag === 0 ? C.emerald : p.lag > 6 ? C.amber : C.blue} />
                <span style={{ fontFamily: "var(--mono)", fontSize: 12.5, color: C.textHi, width: 90 }}>{p.name}</span>
                <div className="bar-track" style={{ flex: 1 }}>
                  <div className="bar-fill" style={{ width: `${100 - Math.min(100, (p.lag / 12) * 100)}%`, background: p.lag === 0 ? C.emerald : p.lag > 6 ? C.amber : C.blue }} />
                </div>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: C.dim, width: 70, textAlign: "right" }}>seq {p.sequence}</span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: p.lag === 0 ? C.emerald : C.amber, width: 52, textAlign: "right" }}>lag {p.lag}</span>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Pipeline & roles" icon="Workflow">
          <div style={{ display: "flex", alignItems: "stretch", gap: 0, flexWrap: "wrap" }}>
            {pipeline.map((s, i) => (
              <div key={s.stage} style={{ display: "contents" }}>
                <div style={{ flex: 1, minWidth: 84, textAlign: "center", padding: "4px 2px" }}>
                  <div style={{ width: 34, height: 34, borderRadius: 9, margin: "0 auto 7px", display: "grid", placeItems: "center", background: "color-mix(in oklab,var(--emerald) 12%,transparent)", border: "1px solid color-mix(in oklab,var(--emerald) 26%,transparent)" }}>
                    <Icon name={(["ClipboardList", "Send", "Bot", "ScanSearch", "GitMerge", "BrainCircuit"] as const)[i] ?? "Circle"} size={15} color={C.emerald} />
                  </div>
                  <div style={{ fontSize: 11.5, color: C.textHi, fontWeight: 500 }}>{s.stage}</div>
                  <div style={{ fontSize: 10.5, color: C.dim, fontFamily: "var(--mono)" }}>{s.role}</div>
                </div>
                {i < pipeline.length - 1 && (
                  <div style={{ display: "flex", alignItems: "center", color: C.faint }}>
                    <Icon name="ChevronRight" size={14} color={C.faint} />
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="Budget — spend over 24h" icon="DollarSign" action={<span className="tag">{money(budget.cumulative)} cumulative</span>}>
        <AreaTrend points={budget.points} accessor={(p) => p.cumulative} color={C.accent} height={170} yFormat={(v) => "$" + Math.round(v)} />
      </Panel>
    </div>
  );
}
