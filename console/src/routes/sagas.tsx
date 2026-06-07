// src/routes/sagas.tsx — SagasScreen ported from prototype/screens-rest.jsx (SAGAS section only)
import { useState, useMemo } from "react";
import { useSagas } from "@/hooks";
import { useNav } from "@/lib/drawer";
import { C, sagaMeta } from "@/lib/theme";
import { money } from "@/lib/format";
import { Panel, Kpi, DataTable, Tabs, SagaStateBadge, AxisChip } from "@/components/primitives";
import { Icon } from "@/components/Icon";
import type { ColumnDef } from "@/components/primitives";
import type { Saga } from "@/domain/models";

export default function SagasScreen() {
  const nav = useNav();
  const sagas = useSagas().data ?? [];
  const [view, setView] = useState("board");

  const lanes = [
    "RUNNING",
    "AWAITING_CRITIC",
    "REVISING",
    "MERGED",
    "COMPENSATED",
    "ABANDONED",
    "QUARANTINED",
  ] as const;

  const byState = (s: string) => sagas.filter((x) => x.state === s);

  const columns = useMemo<ColumnDef<Saga>[]>(
    () => [
      {
        id: "saga",
        header: "Saga",
        sortVal: (s) => s.saga_id,
        cell: (s) => (
          <div>
            <div style={{ fontFamily: "var(--mono)", color: C.accent, fontSize: 12 }}>
              {s.saga_id}
            </div>
            <div
              style={{
                fontSize: 11.5,
                color: C.textMid,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                maxWidth: 320,
              }}
            >
              #{s.issue.number} {s.issue.title}
            </div>
          </div>
        ),
      },
      {
        id: "axis",
        header: "Axis",
        cell: (s) => <AxisChip axis={s.issue.axis} short />,
      },
      {
        id: "state",
        header: "State",
        sortVal: (s) => s.state,
        cell: (s) => <SagaStateBadge state={s.state} size="sm" />,
      },
      {
        id: "rounds",
        header: "Repairs",
        align: "right",
        sortVal: (s) => s.repair_rounds,
        cell: (s) => (
          <span
            style={{
              fontFamily: "var(--mono)",
              color: s.repair_rounds > 1 ? C.amber : C.textMid,
            }}
          >
            {s.repair_rounds}
          </span>
        ),
      },
      {
        id: "cost",
        header: "Cost",
        align: "right",
        sortVal: (s) => s.cost_usd,
        cell: (s) => (
          <span style={{ fontFamily: "var(--mono)", color: C.emerald }}>{money(s.cost_usd)}</span>
        ),
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

  return (
    <div className="page page-wide">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 14,
        }}
      >
        <div
          className="grid"
          style={{ gridTemplateColumns: "repeat(4,1fr)", gap: 12, flex: 1, maxWidth: 720 }}
        >
          <Kpi
            label="In flight"
            value={
              byState("RUNNING").length +
              byState("AWAITING_CRITIC").length +
              byState("REVISING").length
            }
            icon="LoaderCircle"
            color={C.accent}
          />
          <Kpi
            label="Merged"
            value={byState("MERGED").length}
            icon="GitMerge"
            color={C.emerald}
          />
          <Kpi
            label="Abandoned"
            value={byState("ABANDONED").length}
            icon="CircleSlash"
            color={C.textDim}
          />
          <Kpi
            label="Quarantined"
            value={byState("QUARANTINED").length}
            icon="ShieldAlert"
            color={byState("QUARANTINED").length ? C.red : C.emerald}
          />
        </div>
        <Tabs
          tabs={[
            { id: "board", label: "Board", icon: "LayoutGrid" },
            { id: "table", label: "Table", icon: "Table" },
          ]}
          value={view}
          onChange={setView}
        />
      </div>

      {view === "table" ? (
        <Panel pad={false} title="All sagas" icon="Workflow">
          <DataTable<Saga>
            columns={columns}
            rows={sagas}
            rowKey={(s) => s.saga_id}
            onRow={(s) => nav.open("saga", s)}
            initialSort={{ id: "state", dir: "asc" }}
          />
        </Panel>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(7, minmax(170px,1fr))",
            gap: 12,
            overflowX: "auto",
            paddingBottom: 8,
          }}
        >
          {lanes.map((st) => {
            const m = sagaMeta(st);
            const items = byState(st);
            return (
              <div key={st} style={{ minWidth: 170 }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    marginBottom: 9,
                    paddingInline: 2,
                  }}
                >
                  <Icon name={m.icon} size={13} color={m.color} />
                  <span style={{ fontSize: 11.5, fontWeight: 600, color: C.textMid }}>
                    {m.label}
                  </span>
                  <span
                    style={{
                      marginLeft: "auto",
                      fontFamily: "var(--mono)",
                      fontSize: 11,
                      color: C.dim,
                    }}
                  >
                    {items.length}
                  </span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {items.length === 0 ? (
                    <div
                      style={{
                        fontSize: 11,
                        color: C.faint,
                        padding: "12px 8px",
                        textAlign: "center",
                        border: "1px dashed var(--border)",
                        borderRadius: 9,
                      }}
                    >
                      empty
                    </div>
                  ) : (
                    items.map((s) => (
                      <button
                        key={s.saga_id}
                        onClick={() => nav.open("saga", s)}
                        className="focus-ring"
                        style={{
                          textAlign: "left",
                          background: C.elevated,
                          border: "1px solid var(--border)",
                          borderRadius: 10,
                          padding: "10px 11px",
                          borderTop: `2px solid ${m.color}`,
                        }}
                      >
                        <div
                          style={{
                            fontFamily: "var(--mono)",
                            fontSize: 11,
                            color: C.accent,
                            marginBottom: 4,
                          }}
                        >
                          #{s.issue.number}
                        </div>
                        <div
                          style={{
                            fontSize: 12,
                            color: C.textHi,
                            lineHeight: 1.35,
                            marginBottom: 8,
                            display: "-webkit-box",
                            WebkitLineClamp: 2,
                            WebkitBoxOrient: "vertical",
                            overflow: "hidden",
                          }}
                        >
                          {s.issue.title}
                        </div>
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            fontFamily: "var(--mono)",
                            fontSize: 10.5,
                          }}
                        >
                          <span style={{ color: s.repair_rounds > 1 ? C.amber : C.dim }}>
                            r{s.repair_rounds}
                          </span>
                          <span style={{ color: C.emerald }}>{money(s.cost_usd)}</span>
                        </div>
                      </button>
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
