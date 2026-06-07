import { useScorecard } from "@/hooks";
import { useFrontier } from "@/hooks";
import { C } from "@/lib/theme";
import { duration, money } from "@/lib/format";
import { Panel, Banner, NotMeasured, TrendArrow, EmptyState } from "@/components/primitives";
import { KrTrendChart } from "@/components/charts";
import type { ScorecardPoint } from "@/domain/models";

export default function ScorecardScreen() {
  const { data: scorecard } = useScorecard();
  const { data: frontier } = useFrontier();

  if (!scorecard || !frontier)
    return (
      <div className="page">
        <EmptyState icon="LoaderCircle" color={C.accent} title="Loading…" />
      </div>
    );

  const h = scorecard.history;

  const metric = (
    label: string,
    value: string | number,
    target: number,
    color: string,
    accessor: (p: ScorecardPoint) => number | null,
    yFormat: (v: number) => string,
    goodUp: boolean,
    sub?: string,
  ) => {
    const points = h.map((p) => ({ idx: p.idx, value: accessor(p) }));
    return (
      <Panel
        title={label}
        icon={goodUp ? "TrendingUp" : "TrendingDown"}
        action={
          <span style={{ fontFamily: "var(--mono)", fontSize: 16, fontWeight: 600, color }}>
            {value}
          </span>
        }
      >
        {sub && <div style={{ fontSize: 11.5, color: C.dim, marginBottom: 6 }}>{sub}</div>}
        <KrTrendChart
          points={points}
          target={target}
          height={170}
          color={color}
          yFormat={yFormat}
          targetLabel="target"
          goodUp={goodUp}
        />
      </Panel>
    );
  };

  return (
    <div className="page page-wide">
      <div style={{ marginBottom: 16 }}>
        <Banner
          tone="info"
          icon="Target"
          title="The self-improvement claim, made falsifiable"
          action={<span className="tag">Objective · KR</span>}
        >
          {frontier.objective} — measured against KR{" "}
          <b style={{ color: C.textHi }}>{frontier.key_result}</b>
        </Banner>
      </div>

      {/* hero metric: first-pass acceptance vs KR target */}
      <div style={{ marginBottom: 14 }}>
        <Panel
          title="First-pass critic acceptance — vs Key Result target"
          icon="ShieldCheck"
          action={
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <TrendArrow delta={4} goodUp suffix="pt" />
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 22,
                  fontWeight: 600,
                  color: C.accent,
                }}
              >
                {Math.round((scorecard.first_pass_critic_acceptance_rate ?? 0) * 100)}%
              </span>
            </div>
          }
        >
          <div style={{ fontSize: 12.5, color: C.textMid, marginBottom: 8, lineHeight: 1.5 }}>
            Share of PRs the critic approves on the first round — the single number that says
            whether the machine is getting better. The first four windows are hatched: not enough
            merges had accrued to measure honestly.
          </div>
          <KrTrendChart
            points={h.map((p) => ({ idx: p.idx, value: p.first_pass }))}
            target={frontier.kr_target}
            height={240}
          />
        </Panel>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(2,1fr)", marginBottom: 14 }}>
        {metric(
          "Mean repair rounds to converge",
          (scorecard.mean_repair_rounds_to_converge ?? 0).toFixed(2),
          1.0,
          C.amber,
          (p) => p.repair_rounds,
          (v) => v.toFixed(1),
          false,
          "Lower is better — rounds of critic ↔ worker before green",
        )}
        {metric(
          "Mean lead time",
          duration(scorecard.mean_lead_time_seconds),
          3600,
          C.blue,
          (p) => p.lead_time,
          (v) => Math.round(v / 60) + "m",
          false,
          "Dispatch → merge, in minutes",
        )}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(2,1fr)", marginBottom: 14 }}>
        {metric(
          "Cost per merged PR",
          money(scorecard.cost_per_merged_pr),
          2.5,
          C.emerald,
          (p) => p.cost_per_merge,
          (v) => "$" + v.toFixed(1),
          false,
          "Is it burning money productively?",
        )}
        <Panel
          title="Deliberately not measured"
          icon="CircleDashed"
          action={<span className="tag">honesty</span>}
        >
          <div style={{ fontSize: 12.5, color: C.textMid, marginBottom: 12, lineHeight: 1.5 }}>
            The console refuses to assert improvement it can't measure. These read as a designed
            state — never a fake 0 or a broken chart.
          </div>
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <NotMeasured
              label="Sev2 regeneration rate"
              reason="not instrumented"
              detail={scorecard.nulls.sev2_regeneration_rate?.detail}
            />
            <NotMeasured
              label="Abandonment rate"
              reason="not yet measured"
              detail={scorecard.nulls.abandonment_rate?.detail}
            />
          </div>
        </Panel>
      </div>
    </div>
  );
}
