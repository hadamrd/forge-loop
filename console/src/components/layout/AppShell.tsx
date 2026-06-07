// AppShell — sidebar nav + topbar + routed content + drawer host.
// Root-route component (renders <Outlet/>). Ported from prototype/app.jsx; routing via TanStack
// Router, data via Query hooks. Screens (inside <Outlet/>) get the drawer via useNav().
import { Outlet, useLocation, useNavigate } from "@tanstack/react-router";
import { Icon } from "@/components/Icon";
import { DrawerProvider } from "@/lib/drawer";
import { useUi } from "@/lib/ui";
import { color } from "@/lib/theme";
import { useWorkers } from "@/hooks";
import { usePRs } from "@/hooks";
import { useLoopStatus } from "@/hooks";
import { DrawerHost } from "@/components/drawers/DrawerHost";

const C = color;

type NavEntry = { sec: string } | { id: string; label: string; icon: string };
const NAV: NavEntry[] = [
  { sec: "Mission" },
  { id: "overview", label: "Mission Control", icon: "LayoutDashboard" },
  { id: "stream", label: "Live Event Stream", icon: "Radio" },
  { sec: "Execution" },
  { id: "workers", label: "Workers", icon: "Bot" },
  { id: "prs", label: "PRs & Critic", icon: "GitPullRequest" },
  { id: "sagas", label: "Sagas", icon: "Workflow" },
  { sec: "Self-improvement" },
  { id: "scorecard", label: "Scorecard", icon: "TrendingUp" },
  { id: "frontier", label: "Frontier", icon: "Telescope" },
  { id: "memory", label: "Memory", icon: "BrainCircuit" },
  { sec: "Planning" },
  { id: "backlog", label: "Backlog & Axes", icon: "ListTodo" },
  { id: "manifestos", label: "Manifestos", icon: "ScrollText" },
  { sec: "System" },
  { id: "health", label: "Control-plane health", icon: "Activity" },
];

const TITLES: Record<string, string> = {
  overview: "Mission Control", stream: "Live Event Stream", workers: "Workers",
  prs: "PRs & Critic", sagas: "Sagas", scorecard: "Scorecard", frontier: "Frontier",
  memory: "Memory", backlog: "Backlog & Axes", manifestos: "Manifestos", health: "Control-plane health",
};

const idToPath = (id: string) => (id === "overview" ? "/" : `/${id}`);
const pathToId = (path: string) => (path === "/" ? "overview" : path.replace(/^\//, ""));

function PauseToggle() {
  const { paused, setPaused } = useUi();
  return (
    <button className="btn" onClick={() => setPaused(!paused)} title={paused ? "Resume live feed" : "Pause live feed"}>
      <Icon name={paused ? "Play" : "Pause"} size={13} color={paused ? C.amber : C.mid} />
      {paused ? "Paused" : "Live"}
    </button>
  );
}

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const currentId = pathToId(location.pathname);

  const { data: workers = [] } = useWorkers();
  const { data: prs = [] } = usePRs();
  const { data: status } = useLoopStatus();

  const activeWorkers = workers.filter((w) => ["RUNNING", "AWAITING_CRITIC", "REVISING"].includes(w.state)).length;
  const openPrs = prs.filter((p) => p.state === "open").length;
  const blocking = prs.filter((p) => p.state === "open" && p.labels.includes("critic:blocking")).length;

  const badge = (id: string) => {
    if (id === "workers") return <span className="nav-badge live">{activeWorkers}</span>;
    if (id === "prs") return blocking ? <span className="nav-badge alert">{openPrs}</span> : <span className="nav-badge">{openPrs}</span>;
    if (id === "stream")
      return (
        <span className="nav-badge live">
          <span className="dot-pulse" style={{ background: C.emerald, width: 5, height: 5 }} />
        </span>
      );
    return null;
  };

  return (
    <DrawerProvider>
      <div className="app">
        <nav className="sidebar">
          <div className="brand">
            <span className="brand-mark">
              <Icon name="Hexagon" size={15} color="#04211f" strokeWidth={2.5} />
            </span>
            <div style={{ minWidth: 0 }}>
              <div className="brand-name">forge-loop</div>
              <div className="brand-sub">operator console</div>
            </div>
          </div>
          <div className="nav">
            {NAV.map((n, i) =>
              "sec" in n ? (
                <div key={"s" + i} className="nav-sec">{n.sec}</div>
              ) : (
                <button
                  key={n.id}
                  className={"nav-item" + (currentId === n.id ? " on" : "")}
                  onClick={() => void navigate({ to: idToPath(n.id) })}
                >
                  <Icon name={n.icon} size={16} color={currentId === n.id ? C.accent : C.dim} />
                  {n.label}
                  {badge(n.id)}
                </button>
              ),
            )}
          </div>
          <div style={{ padding: "12px 14px", borderTop: "1px solid var(--hair)", display: "flex", alignItems: "center", gap: 9 }}>
            <span className="dot-pulse" style={{ background: C.emerald }} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 11.5, color: C.mid, fontWeight: 500 }}>{status?.boot.version ?? "forge"}</div>
              <div style={{ fontSize: 10.5, color: C.faint, fontFamily: "var(--mono)" }}>mock api · live</div>
            </div>
          </div>
        </nav>

        <main className="main">
          <header className="topbar">
            <h1>{TITLES[currentId] ?? "forge-loop"}</h1>
            <span className="crumb">/ forge-loop</span>
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
              <span className="tag" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Icon name="Hash" size={11} color={C.dim} />seq {status?.sequence ?? "—"}
              </span>
              <PauseToggle />
              <button className="btn">
                <Icon name="Command" size={13} color={C.mid} />Search
              </button>
            </div>
          </header>
          <div className="content">
            <Outlet />
          </div>
        </main>
      </div>
      <DrawerHost />
    </DrawerProvider>
  );
}
