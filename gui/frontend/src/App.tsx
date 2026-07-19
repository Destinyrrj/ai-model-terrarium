import { lazy, Suspense } from "react";
import { AppShell } from "./components/Primitives";
import { LoadingBlock } from "./components/Primitives";
import { DashboardPage } from "./pages/DashboardPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { useRoute } from "./router";

const ConfigsPage = lazy(() => import("./pages/ConfigsPage").then((module) => ({ default: module.ConfigsPage })));
const RunPage = lazy(() => import("./pages/RunPage").then((module) => ({ default: module.RunPage })));

export function App(): React.JSX.Element {
  const route = useRoute();
  const runMatch = /^\/runs\/([^/]+)\/?$/.exec(route.pathname);
  let page: React.ReactNode;

  if (route.pathname === "/" || route.pathname === "/dashboard") {
    page = <DashboardPage />;
  } else if (route.pathname === "/configs" || route.pathname.startsWith("/configs/")) {
    const selected = route.pathname.startsWith("/configs/")
      ? decodeURIComponent(route.pathname.slice("/configs/".length))
      : undefined;
    page = <ConfigsPage initialName={selected} />;
  } else if (runMatch?.[1]) {
    page = <RunPage runName={decodeURIComponent(runMatch[1])} />;
  } else {
    page = <NotFoundPage />;
  }

  return (
    <AppShell pathname={route.pathname}>
      <Suspense fallback={<div className="page"><LoadingBlock label="Загружаем исследовательский модуль" /></div>}>
        {page}
      </Suspense>
    </AppShell>
  );
}
