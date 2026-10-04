// A full-screen "Starting <app>…" page shown in Gaming Mode right before
// switching, while the launch is staged behind it. Gamescope is stopped
// quickly once the switch starts, which cuts the screen to black; the page
// fades out first so the cut lands on black, then the desktop session's own
// loading screen takes over.
import { routerHook } from "@decky/api";
import { Navigation } from "@decky/ui";
import { useEffect, useState } from "react";

export const LAUNCHING_ROUTE = "/quickscope/launching";

let appName = "";
let fading = false;
const fadeListeners = new Set<() => void>();

function LaunchingPage() {
  const [faded, setFaded] = useState(fading);
  useEffect(() => {
    const listener = () => setFaded(true);
    fadeListeners.add(listener);
    return () => {
      fadeListeners.delete(listener);
    };
  }, []);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        background: "black",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {/* Only the content fades: the black backdrop stays over Steam's UI. */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "24px",
          opacity: faded ? 0 : 1,
          transition: "opacity 0.25s ease-in",
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
    </div>
  );
}

/** Show the launching page; resolves once it has had a moment to render. */
export function showLaunchingPage(name: string): Promise<void> {
  appName = name;
  fading = false;
  // Launching from the Quick Access menu would otherwise open the page
  // behind the menu, where it can't be seen.
  Navigation.CloseSideMenus();
  Navigation.Navigate(LAUNCHING_ROUTE);
  return new Promise((resolve) => setTimeout(resolve, 250));
}

/** Fade the page to black as the switch starts, just ahead of Gamescope stopping. */
export function fadeLaunchingPage() {
  fading = true;
  fadeListeners.forEach((listener) => listener());
}

export function hideLaunchingPage() {
  Navigation.NavigateBack();
}

/** Register the page's route. Returns a function that removes it. */
export function addLaunchingRoute(): () => void {
  routerHook.addRoute(LAUNCHING_ROUTE, LaunchingPage, { exact: true });
  return () => routerHook.removeRoute(LAUNCHING_ROUTE);
}
