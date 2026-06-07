import { useState } from "react";
import { useMemory } from "@/hooks";
import { useNow } from "@/lib/ui";
import { C, memoryMeta } from "@/lib/theme";
import { relativeTime, pct } from "@/lib/format";
import { Tabs, Pill } from "@/components/primitives";
import { Icon } from "@/components/Icon";

export default function MemoryScreen() {
  const memory = useMemory().data ?? [];
  const now = useNow();
  const [kind, setKind] = useState("all");

  const kinds = [
    { id: "all", label: "All", icon: "Library" },
    { id: "procedural", label: "Procedural", icon: "Workflow" },
    { id: "episodic", label: "Episodic", icon: "BookMarked" },
    { id: "rejected_path", label: "Rejected", icon: "SignpostBig" },
  ];

  const items = memory.filter((m) => kind === "all" || m.kind === kind);
  const findById = (id: string) => memory.find((m) => m.id === id);

  return (
    <div className="page page-wide">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <Tabs tabs={kinds} value={kind} onChange={setKind} />
        <span
          style={{
            marginLeft: "auto",
            fontSize: 12,
            color: C.dim,
            fontFamily: "var(--mono)",
          }}
        >
          {memory.filter((m) => m.superseded_by).length} superseded · {memory.length} total
        </span>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "repeat(3,1fr)" }}>
        {items.map((m) => {
          const km = memoryMeta(m.kind);
          const superseded = !!m.superseded_by;
          const by = m.superseded_by ? findById(m.superseded_by) : undefined;
          return (
            <div
              key={m.id}
              style={{
                background: C.panel,
                border: "1px solid var(--border)",
                borderRadius: 11,
                padding: 14,
                opacity: superseded ? 0.7 : 1,
                position: "relative",
                overflow: "hidden",
              }}
            >
              <span
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  right: 0,
                  height: 2,
                  background: km.color,
                }}
              />
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 8,
                }}
              >
                <Pill icon={km.icon} color={km.color} size="sm">
                  {km.label}
                </Pill>
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    fontFamily: "var(--mono)",
                    fontSize: 11,
                    color:
                      m.confidence > 0.8
                        ? C.emerald
                        : m.confidence > 0.5
                          ? C.amber
                          : C.dim,
                  }}
                >
                  <Icon
                    name="Gauge"
                    size={11}
                    color={
                      m.confidence > 0.8
                        ? C.emerald
                        : m.confidence > 0.5
                          ? C.amber
                          : C.dim
                    }
                  />
                  {pct(m.confidence)}
                </span>
              </div>
              <div
                style={{
                  fontSize: 13.5,
                  fontWeight: 600,
                  color: C.textHi,
                  marginBottom: 6,
                  textDecoration: superseded ? "line-through" : "none",
                  textDecorationColor:
                    "color-mix(in oklab,var(--red) 45%,transparent)",
                }}
              >
                {m.title}
              </div>
              <div
                style={{
                  fontSize: 12,
                  color: C.textMid,
                  lineHeight: 1.5,
                  marginBottom: 10,
                }}
              >
                {m.body}
              </div>
              {superseded && by && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    fontSize: 11.5,
                    color: C.amber,
                    marginBottom: 8,
                  }}
                >
                  <Icon name="ArrowRight" size={12} color={C.amber} />
                  superseded by &quot;{by.title}&quot;
                </div>
              )}
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 5,
                  alignItems: "center",
                }}
              >
                {m.evidence_refs.map((r) => (
                  <span key={r} className="tag" style={{ fontSize: 10.5 }}>
                    <Icon
                      name="Paperclip"
                      size={10}
                      color={C.dim}
                      style={{ verticalAlign: -1, marginRight: 3 }}
                    />
                    {r}
                  </span>
                ))}
                <span
                  style={{
                    marginLeft: "auto",
                    fontSize: 10.5,
                    color: C.faint,
                    fontFamily: "var(--mono)",
                  }}
                >
                  {relativeTime(m.created_at, now)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
