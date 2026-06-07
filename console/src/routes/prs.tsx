// src/routes/prs.tsx — PrsScreen ported from prototype/screens-prs.jsx
import { useState, useMemo } from "react";
import { usePRs } from "@/hooks";
import { useNav } from "@/lib/drawer";
import { C, verdictMeta } from "@/lib/theme";
import { Panel, Kpi, Banner, EmptyState, DataTable, Tabs, LabelChip, SevBadge, Pill } from "@/components/primitives";
import { Icon } from "@/components/Icon";
import type { ColumnDef } from "@/components/primitives";
import type { PullRequest } from "@/domain/models";

export default function PrsScreen() {
  const nav = useNav();
  const prs = usePRs().data ?? [];
  const [tab, setTab] = useState("open");

  const open = useMemo(() => prs.filter((p) => p.state === "open"), [prs]);
  const blocking = useMemo(
    () => open.filter((p) => p.labels.includes("critic:blocking")),
    [open],
  );
  const suspicious = useMemo(() => open.filter((p) => p.review.suspicious), [open]);
  const clean = useMemo(
    () => open.filter((p) => !p.labels.includes("critic:blocking") && !p.review.suspicious),
    [open],
  );
  const merged = useMemo(() => prs.filter((p) => p.state === "merged"), [prs]);

  const tabs = useMemo(
    () => [
      { id: "open", label: "Open", icon: "GitPullRequest", count: open.length },
      { id: "blocking", label: "Blocking", icon: "Ban", count: blocking.length },
      { id: "suspicious", label: "Suspicious", icon: "Eye", count: suspicious.length },
      { id: "merged", label: "Merged", icon: "GitMerge", count: merged.length },
    ],
    [open.length, blocking.length, suspicious.length, merged.length],
  );

  const rows = useMemo<PullRequest[]>(() => {
    const map: Record<string, PullRequest[]> = { open, blocking, suspicious, merged };
    return map[tab] ?? [];
  }, [tab, open, blocking, suspicious, merged]);

  const columns = useMemo<ColumnDef<PullRequest>[]>(
    () => [
      {
        id: "pr",
        header: "PR",
        sortVal: (p) => p.number,
        cell: (p) => (
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <span style={{ fontFamily: "var(--mono)", color: C.accent, fontSize: 12 }}>
                #{p.number}
              </span>
              <span
                style={{
                  fontSize: 13,
                  color: C.textHi,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  maxWidth: 340,
                }}
              >
                {p.title}
              </span>
            </div>
            <div
              style={{ fontFamily: "var(--mono)", fontSize: 11, color: C.dim, marginTop: 2 }}
            >
              {p.branch}
            </div>
          </div>
        ),
      },
      {
        id: "labels",
        header: "Labels",
        cell: (p) => (
          <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
            {p.labels.map((l) => (
              <LabelChip key={l} label={l} />
            ))}
          </div>
        ),
      },
      {
        id: "diff",
        header: "Diff",
        align: "right",
        sortVal: (p) => p.additions + p.deletions,
        cell: (p) => (
          <span style={{ fontFamily: "var(--mono)", fontSize: 12 }}>
            <span style={{ color: C.emerald }}>+{p.additions}</span>{" "}
            <span style={{ color: C.red }}>−{p.deletions}</span>
          </span>
        ),
      },
      {
        id: "verdict",
        header: "Critic",
        sortVal: (p) => p.review.verdict,
        cell: (p) => {
          const v = verdictMeta(p.review.verdict);
          return (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                color: v.color,
                fontSize: 12,
              }}
            >
              <Icon name={v.icon} size={13} color={v.color} />
              {v.label}
              <span style={{ color: C.dim, fontFamily: "var(--mono)", fontSize: 11 }}>
                r{p.review.round}
              </span>
            </span>
          );
        },
      },
      {
        id: "sev",
        header: "Findings",
        align: "right",
        sortVal: (p) =>
          p.review.sev_counts.sev1 * 100 +
          p.review.sev_counts.sev2 * 10 +
          p.review.sev_counts.sev3,
        cell: (p) => {
          const s = p.review.sev_counts;
          const total = s.sev1 + s.sev2 + s.sev3;
          if (total === 0)
            return (
              <Pill icon="Check" color={C.emerald} size="sm">
                clean
              </Pill>
            );
          return (
            <span style={{ display: "inline-flex", gap: 5, justifyContent: "flex-end" }}>
              {s.sev1 > 0 && <SevBadge sev="sev1" count={s.sev1} />}
              {s.sev2 > 0 && <SevBadge sev="sev2" count={s.sev2} />}
              {s.sev3 > 0 && <SevBadge sev="sev3" count={s.sev3} />}
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
    [],
  );

  const activeTab = tabs.find((t) => t.id === tab)!;

  return (
    <div className="page">
      <div className="grid" style={{ gridTemplateColumns: "repeat(4,1fr)", marginBottom: 16 }}>
        <Kpi label="Open PRs" value={open.length} icon="GitPullRequest" color={C.blue} />
        <Kpi
          label="Blocking"
          value={blocking.length}
          icon="Ban"
          color={blocking.length ? C.red : C.emerald}
          sub={blocking.length ? "unresolved sev2+" : "nothing blocked"}
        />
        <Kpi
          label="Suspicious holds"
          value={suspicious.length}
          icon="Eye"
          color={suspicious.length ? C.amber : C.emerald}
          sub={suspicious.length ? "passes tests, held" : "no holds"}
        />
        <Kpi label="Clean & mergeable" value={clean.length} icon="ShieldCheck" color={C.emerald} />
      </div>

      {suspicious.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <Banner
            tone="warn"
            icon="Eye"
            title={`#${suspicious[0].number} — suspicious but passing`}
            action={
              <button className="btn" onClick={() => nav.open("pr", suspicious[0])}>
                Inspect
              </button>
            }
          >
            {suspicious[0].review.suspicious_reason}
          </Banner>
        </div>
      )}

      <div style={{ marginBottom: 12 }}>
        <Tabs tabs={tabs} value={tab} onChange={setTab} />
      </div>

      <Panel pad={false} title={activeTab.label + " PRs"} icon="GitPullRequest">
        <DataTable<PullRequest>
          columns={columns}
          rows={rows}
          rowKey={(p) => p.number}
          onRow={(p) => nav.open("pr", p)}
          initialSort={{ id: "sev", dir: "desc" }}
          empty={
            tab === "blocking" ? (
              <EmptyState
                icon="ShieldCheck"
                title="Nothing blocked — clean run"
                sub="No PR is held on unresolved sev1/sev2 findings."
              />
            ) : tab === "suspicious" ? (
              <EmptyState
                icon="Eye"
                title="No suspicious PRs — clean run"
                sub="Every passing PR also covers its critical paths."
              />
            ) : (
              <EmptyState icon="Inbox" color={C.dim} title="No PRs here" />
            )
          }
        />
      </Panel>
    </div>
  );
}
