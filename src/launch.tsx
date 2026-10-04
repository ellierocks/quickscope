import { ConfirmModal, Router, showModal } from "@decky/ui";
import { toaster } from "@decky/api";

import {
  LaunchMode,
  LaunchSpec,
  MoonlightApp,
  MoonlightHost,
  Settings,
  ShortcutMode,
  cancelPending,
  prepareLaunch,
  switchSession,
} from "./backend";
import { fadeLaunchingPage, hideLaunchingPage, showLaunchingPage } from "./launching";
import { LibraryApp, getShortcutDetails, steamSwitchToDesktop } from "./library";

export const DEFAULT_SHORTCUT_MODE: ShortcutMode = "hybrid";

export const MODE_NAMES: Record<LaunchMode, string> = {
  // Desktop Steam has to start first: about 13 s, so mostly for a game that
  // misbehaves under Gamescope.
  steam: "Steam · slower",
  hybrid: "Hybrid",
  direct: "Direct",
};

export function toast(body: string) {
  toaster.toast({ title: "Quickscope", body });
}

export function effectiveMode(app: LibraryApp, settings: Settings): LaunchMode {
  // Steam games can only be started by Steam.
  if (app.kind === "steam") return "steam";
  return settings.launch_modes[app.appid] ?? DEFAULT_SHORTCUT_MODE;
}

function confirm(title: string, description: string, okText: string): Promise<boolean> {
  return new Promise((resolve) => {
    showModal(
      <ConfirmModal
        strTitle={title}
        strDescription={description}
        strOKButtonText={okText}
        onOK={() => resolve(true)}
        onCancel={() => resolve(false)}
      />,
    );
  });
}

/** Stage a Quickscope launch of `app` and leave Gaming Mode. */
export function launch(app: LibraryApp) {
  return stageAndSwitch(app.name, async () => {
    const spec: LaunchSpec = { appid: app.appid, name: app.name, kind: app.kind };
    if (app.kind === "shortcut") {
      const details = await getShortcutDetails(app.appid);
      if (details) {
        spec.exe = details.exe;
        spec.start_dir = details.startDir;
        spec.launch_options = details.launchOptions;
      }
    }
    return spec;
  });
}

/** Stream a host app straight away with Moonlight, skipping its menus. */
export function launchStream(host: MoonlightHost, app: MoonlightApp) {
  const name = app.name.trim();
  return stageAndSwitch(name, async () => ({ appid: app.id, name, kind: "stream", host: host.name, app: app.name }));
}

async function stageAndSwitch(name: string, buildSpec: () => Promise<LaunchSpec>) {
  const running = Router.MainRunningApp;
  if (running) {
    const ok = await confirm(
      "Close running game?",
      `${running.display_name} is running and will be closed when Gaming Mode exits.`,
      "Launch anyway",
    );
    if (!ok) return;
  }

  // Straight to the launching page; staging (about 2 s) happens behind it.
  await showLaunchingPage(name);
  try {
    const spec = await buildSpec();
    const staged = await prepareLaunch(spec);
    if (!staged.ok) {
      hideLaunchingPage();
      toast(`Couldn't stage launch: ${staged.error}`);
      return;
    }
    if (spec.kind === "shortcut" && staged.mode === "steam") {
      toast(`${name}: launching through Steam, couldn't read the shortcut`);
    }

    fadeLaunchingPage();
    const switched = await switchSession();
    if (!switched.ok) {
      // Steam's own Switch to Desktop as a last resort; it picks its own session.
      try {
        if (steamSwitchToDesktop()) return;
      } catch (e) {
        console.error("Quickscope: SwitchToDesktop failed", e);
      }
      hideLaunchingPage();
      await cancelPending();
      toast(`Couldn't switch to desktop: ${switched.error}`);
    }
  } catch (e) {
    hideLaunchingPage();
    throw e;
  }
}
