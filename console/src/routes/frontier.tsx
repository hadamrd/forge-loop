import { useFrontier, useEvents } from "@/hooks";
import { useNow } from "@/lib/ui";
import { C, eventMeta } from "@/lib/theme";
import { relativeTime } from "@/lib/format";
import { Panel, EmptyState } from "@/components/primitives";
import { Icon } from "@/components/Icon";

export default function FrontierScreen() {
  const { data: frontier } = useFrontier();
  const { events } = useEvents();
  const now = useNow();

  if (!frontier)
    return (
      <div className="page">
        <EmptyState icon="LoaderCircle" color={C.accent} title="Loading…" />
      </div>
    );

  const f = frontier;
  const advances = events
    .filter((e) => e.kind === "frontier.advanced" || e.kind === "vision.updated")
    .slice(-6)
    .reverse();

  return (
    <div
      className="page page-wide"
      style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr", gap: 16, alignItems: "start" }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Panel
          title="Frontier cursor"
          icon="Telescope"
          action={
            <span className="tag">
              .forge/frontier.yaml · v{f.version}
            </span>
          }
        >
          <div
            style={{
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: ".06em",
              color: C.faint,
              fontWeight: 600,
              marginBottom: 5,
            }}
          >
            Product goal
          </div>
          <div
            style={{
              fontSize: 14.5,
              color: C.textHi,
              lineHeight: 1.5,
              fontWeight: 500,
              marginBottom: 16,
            }}
          >
            {f.product_goal}
          </div>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <div
              style={{
                background: C.elevated,
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: 13,
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 7 }}
              >
                <Icon name="CircleHelp" size={13} color={C.amber} />
                <span style={{ fontSize: 11.5, fontWeight: 600, color: C.amber }}>
                  Current problem
                </span>
              </div>
              <div style={{ fontSize: 12.5, color: C.textMid, lineHeight: 1.5 }}>
                {f.current_problem}
              </div>
            </div>
            <div
              style={{
                background: C.elevated,
                border: "1px solid var(--border)",
                borderRadius: 10,
                padding: 13,
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 7 }}
              >
                <Icon name="MoveRight" size={13} color={C.accent} />
                <span style={{ fontSize: 11.5, fontWeight: 600, color: C.accent }}>
                  Next expansion
                </span>
              </div>
              <div style={{ fontSize: 12.5, color: C.textMid, lineHeight: 1.5 }}>
                {f.next_expansion}
              </div>
            </div>
          </div>
          <div
            style={{
              marginTop: 14,
              padding: 13,
              borderRadius: 10,
              border:
                "1px solid color-mix(in oklab,var(--emerald) 22%,var(--border))",
              background: "color-mix(in oklab,var(--emerald) 6%,transparent)",
            }}
          >
            <div
              style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}
            >
              <Icon name="Target" size={13} color={C.emerald} />
              <span style={{ fontSize: 11.5, fontWeight: 600, color: C.emerald }}>
                Objective &amp; Key Result
              </span>
            </div>
            <div style={{ fontSize: 13.5, color: C.textHi, marginBottom: 7 }}>
              {f.objective}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div className="bar-track" style={{ flex: 1, height: 7 }}>
                <div
                  className="bar-fill"
                  style={{
                    width: `${(f.kr_current / f.kr_target) * 100}%`,
                    background: `linear-gradient(90deg,${C.accent},${C.emerald})`,
                  }}
                />
              </div>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 12,
                  color: C.textMid,
                }}
              >
                {Math.round(f.kr_current * 100)}% / {Math.round(f.kr_target * 100)}%
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: C.dim, marginTop: 6 }}>{f.key_result}</div>
          </div>
        </Panel>

        <Panel
          title="Rejected paths"
          icon="SignpostBig"
          action={<span className="tag">{f.rejected_paths.length}</span>}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {f.rejected_paths.map((r, i) => (
              <div key={i} style={{ borderLeft: `2px solid ${C.amber}`, paddingLeft: 12 }}>
                <div
                  style={{
                    fontSize: 13,
                    color: C.textHi,
                    fontWeight: 500,
                    marginBottom: 3,
                    textDecoration: "line-through",
                    textDecorationColor:
                      "color-mix(in oklab,var(--amber) 45%,transparent)",
                  }}
                >
                  {r.idea}
                </div>
                <div style={{ fontSize: 12, color: C.textMid, lineHeight: 1.5 }}>
                  {r.reason}
                </div>
                <div
                  style={{
                    fontSize: 11.5,
                    color: C.dim,
                    marginTop: 4,
                    fontFamily: "var(--mono)",
                  }}
                >
                  <Icon
                    name="RotateCcw"
                    size={11}
                    color={C.dim}
                    style={{ verticalAlign: -1, marginRight: 4 }}
                  />
                  revisit if: {r.revisit_if}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Panel title="Active decisions" icon="GitBranchPlus">
          <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            {f.active_decisions.map((d) => (
              <div key={d.id} style={{ display: "flex", gap: 9 }}>
                <span className="tag" style={{ flexShrink: 0, color: C.blue }}>
                  {d.id}
                </span>
                <span style={{ fontSize: 12.5, color: C.textMid, lineHeight: 1.45 }}>
                  {d.text}
                </span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Hot files" icon="Flame">
          <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            {f.hot_files.map((hf, i) => (
              <div key={i}>
                <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: C.accent }}>
                  {hf.ref}
                </div>
                <div style={{ fontSize: 11.5, color: C.dim, marginTop: 2 }}>{hf.why_hot}</div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Open questions" icon="CircleHelp">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {f.open_questions.map((qq, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  gap: 8,
                  fontSize: 12.5,
                  color: C.textMid,
                  lineHeight: 1.45,
                }}
              >
                <span style={{ color: C.faint, fontFamily: "var(--mono)" }}>?</span>
                {qq}
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="Frontier history" icon="History">
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {advances.map((e) => {
              const m = eventMeta(e.kind);
              const payload = e.payload as {
                version?: number;
                next_expansion?: string;
                note?: string;
              };
              return (
                <div
                  key={e.event_id}
                  style={{ display: "flex", alignItems: "center", gap: 9, padding: "6px 0" }}
                >
                  <Icon name={m.icon} size={13} color={m.color} />
                  <span style={{ flex: 1, fontSize: 12, color: C.textMid }}>
                    v{payload.version} ·{" "}
                    {(payload.next_expansion || payload.note || "").slice(0, 46)}
                  </span>
                  <span className="ev-time">
                    {relativeTime(e.occurred_at, now)}
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>
      </div>
    </div>
  );
}
