import { useManifestos } from "@/hooks";
import { Panel, Banner, SevBadge } from "@/components/primitives";
import { Icon } from "@/components/Icon";
import { C, sevMeta } from "@/lib/theme";

export default function ManifestosScreen() {
  const manifestos = useManifestos().data ?? [];
  const groups = [
    { id: "quality", label: "Quality manifesto", icon: "ShieldCheck", desc: "Correctness & safety gates the critic enforces before any merge." },
    { id: "testing", label: "Testing manifesto", icon: "FlaskConical", desc: "Evidence gates — every change must prove it does what it claims." },
  ];
  return (
    <div className="page">
      <div style={{ marginBottom: 16 }}>
        <Banner tone="info" icon="ScrollText" title="The gates that make 'self-improving' mean something">
          Rules the critic checks on every PR. Most were learned from a real failure — the source PR is linked.
        </Banner>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
        {groups.map((g) => {
          const rules = manifestos.filter((m) => m.manifesto === g.id);
          return (
            <Panel key={g.id} title={g.label} icon={g.icon} action={<span className="tag">{rules.length} rules</span>}>
              <div style={{ fontSize: 12, color: C.dim, marginBottom: 12, lineHeight: 1.5 }}>{g.desc}</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {rules.map((r) => {
                  const sm = sevMeta(r.severity);
                  return (
                    <div key={r.id} style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "11px 13px", background: C.elevated }}>
                      <div style={{ display: "flex", alignItems: "flex-start", gap: 9 }}>
                        <Icon name={sm.icon} size={14} color={sm.color} style={{ marginTop: 2, flexShrink: 0 }} />
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 13, color: C.textHi, fontWeight: 500, lineHeight: 1.4 }}>{r.rule}</div>
                          <div style={{ fontSize: 12, color: C.textMid, marginTop: 5, lineHeight: 1.45 }}>{r.rationale}</div>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 7 }}>
                            <SevBadge sev={r.severity} />
                            {r.source_pr && (
                              <span className="tag" style={{ color: C.blue }}>
                                <Icon name="GitPullRequest" size={10} color={C.blue} style={{ verticalAlign: -1, marginRight: 3 }} />
                                {r.source_pr}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
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
