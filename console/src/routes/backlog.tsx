import { useBacklog } from "@/hooks";
import { Panel, Kpi, Pill, LabelChip } from "@/components/primitives";
import { DistBars } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { C, AXIS_META, axisMeta } from "@/lib/theme";

export default function BacklogScreen() {
  const backlog = useBacklog().data ?? [];
  const axisKeys = Object.keys(AXIS_META);
  const distRows: { label: string; color: string; value: number }[] = axisKeys.map((a) => ({
    label: axisMeta(a).short,
    color: axisMeta(a).color,
    value: backlog.filter((i) => i.axis === a).length,
  }));
  const ready = backlog.filter((i) => i.labels.includes("loop:ready")).length;
  const epics = backlog.filter((i) => i.labels.includes("epic"));

  return (
    <div className="page page-wide">
      <div className="grid" style={{ gridTemplateColumns: "1.4fr 1fr", marginBottom: 16 }}>
        <Panel title="Distribution across value axes" icon="ChartColumn" action={<span className="tag">{backlog.length} issues</span>}>
          <DistBars rows={distRows} />
        </Panel>
        <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 12, alignContent: "start" }}>
          <Kpi label="Loop-ready" value={ready} icon="CircleDot" color={C.emerald} sub="dispatchable now" />
          <Kpi label="Epics" value={epics.length} icon="Layers" color={C.violet} />
          <Kpi label="In progress" value={backlog.filter((i) => i.state === "in_progress").length} icon="LoaderCircle" color={C.accent} />
          <Kpi label="Axes" value={axisKeys.length} icon="Grid3x3" color={C.blue} sub="value dimensions" />
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {axisKeys.map((a) => {
          const am = axisMeta(a);
          const items = backlog.filter((i) => i.axis === a);
          return (
            <Panel key={a} title={am.label} action={<span className="tag" style={{ color: am.color }}>{items.length}</span>}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: -4, marginBottom: 10 }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: am.color }} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                {items.map((i) => {
                  const isEpic = i.labels.includes("epic");
                  const isSub = !!i.epic;
                  return (
                    <div key={i.number} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 6px", paddingLeft: isSub ? 26 : 6, borderBottom: "1px solid var(--hair)" }}>
                      {isSub && <Icon name="CornerDownRight" size={13} color={C.faint} style={{ marginLeft: -16 }} />}
                      <Icon
                        name={isEpic ? "Layers" : (i.state === "in_progress" ? "LoaderCircle" : "Circle")}
                        size={13}
                        color={isEpic ? C.violet : (i.state === "in_progress" ? C.accent : C.dim)}
                      />
                      <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: C.dim, width: 38 }}>#{i.number}</span>
                      <span style={{ flex: 1, fontSize: 13, color: C.textHi, fontWeight: isEpic ? 600 : 400 }}>{i.title}</span>
                      {i.labels.filter((l) => l !== "epic").map((l) => <LabelChip key={l} label={l} />)}
                      {i.state === "in_progress" && <Pill icon="LoaderCircle" color={C.accent} size="sm" pulse>active</Pill>}
                    </div>
                  );
                })}
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}
