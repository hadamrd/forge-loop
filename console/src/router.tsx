// Code-based TanStack Router tree. AppShell is the root layout (renders <Outlet/>);
// each screen is one route. Screen components live in src/routes/<id>.tsx (default export).
import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { AppShell } from "@/components/layout/AppShell";

import Overview from "@/routes/overview";
import Stream from "@/routes/stream";
import Workers from "@/routes/workers";
import Prs from "@/routes/prs";
import Sagas from "@/routes/sagas";
import Scorecard from "@/routes/scorecard";
import Frontier from "@/routes/frontier";
import Memory from "@/routes/memory";
import Backlog from "@/routes/backlog";
import Manifestos from "@/routes/manifestos";
import Health from "@/routes/health";

const rootRoute = createRootRoute({ component: AppShell });

const route = (path: string, component: () => React.JSX.Element) =>
  createRoute({ getParentRoute: () => rootRoute, path, component });

const routeTree = rootRoute.addChildren([
  route("/", Overview),
  route("/stream", Stream),
  route("/workers", Workers),
  route("/prs", Prs),
  route("/sagas", Sagas),
  route("/scorecard", Scorecard),
  route("/frontier", Frontier),
  route("/memory", Memory),
  route("/backlog", Backlog),
  route("/manifestos", Manifestos),
  route("/health", Health),
]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
