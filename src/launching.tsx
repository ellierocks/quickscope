// A full-screen "Starting <app>…" page shown in Gaming Mode right before
// switching. Steam's UI is torn down first, but Gamescope keeps showing its
// last frame until it exits, so this page bridges into the desktop session's
// own loading screen instead of leaving a black screen.
import { routerHook } from "@decky/api";
import { Navigation } from "@decky/ui";

export const LAUNCHING_ROUTE = "/quickscope/launching";

let appName = "";

function LaunchingPage() {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        background: "black",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "24px",
      }}
    >
      {/* The same ring as the desktop session's loading screen, so the two read as one. */}
      <style>{"@keyframes quickscope-spin { to { transform: rotate(360deg); } }"}</style>
      <div
        style={{
          width: 40,
          height: 40,
          borderRadius: "50%",
          border: "4px solid #2a2f38",
          borderTopColor: "#1a9fff",
          borderRightColor: "#1a9fff",
          animation: "quickscope-spin 0.9s linear infinite",
        }}
      />
      <div style={{ color: "#d0d0d0", fontSize: "26px" }}>Starting {appName}…</div>
    </div>
  );
}

/** Show the launching page; resolves once it has had a moment to render. */
export function showLaunchingPage(name: string): Promise<void> {
  appName = name;
  // Launching from the Quick Access menu would otherwise open the page
  // behind the menu, where it can't be seen.
  Navigation.CloseSideMenus();
  Navigation.Navigate(LAUNCHING_ROUTE);
  return new Promise((resolve) => setTimeout(resolve, 250));
}

export function hideLaunchingPage() {
  Navigation.NavigateBack();
}

/** Register the page's route. Returns a function that removes it. */
export function addLaunchingRoute(): () => void {
  routerHook.addRoute(LAUNCHING_ROUTE, LaunchingPage, { exact: true });
  return () => routerHook.removeRoute(LAUNCHING_ROUTE);
}
