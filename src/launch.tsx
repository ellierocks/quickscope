import { ConfirmModal, Router, showModal } from "@decky/ui";
import { toaster } from "@decky/api";

import {
  LaunchMode,
  LaunchSpec,
  Settings,
  ShortcutMode,
  cancelPending,
  prepareLaunch,
  switchSession,
} from "./backend";
import { LibraryApp, getShortcutDetails, steamSwitchToDesktop } from "./library";

export const DEFAULT_SHORTCUT_MODE: ShortcutMode = "hybrid";

export const MODE_NAMES: Record<LaunchMode, string> = {
  steam: "Steam",
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
export async function launch(app: LibraryApp) {
  const running = Router.MainRunningApp;
  if (running) {
    const ok = await confirm(
      "Close running game?",
      `${running.display_name} is running and will be closed when Gaming Mode exits.`,
      "Launch anyway",
    );
    if (!ok) return;
  }
  const spec: LaunchSpec = { appid: app.appid, name: app.name, kind: app.kind };
  if (app.kind === "shortcut") {
    const details = await getShortcutDetails(app.appid);
    if (details) {
      spec.exe = details.exe;
      spec.start_dir = details.startDir;
      spec.launch_options = details.launchOptions;
    }
  }

  const staged = await prepareLaunch(spec);
  if (!staged.ok) {
    toast(`Couldn't stage launch: ${staged.error}`);
    return;
  }
  const via =
    app.kind === "shortcut" && staged.mode === "steam"
      ? " (through Steam: couldn't read the shortcut)"
      : staged.mode ? ` (${MODE_NAMES[staged.mode]})` : "";
  toast(`Leaving Gamescope for ${app.name}${via}…`);

  const switched = await switchSession();
  if (!switched.ok) {
    // Steam's own Switch to Desktop as a last resort; it picks its own session.
    try {
      if (steamSwitchToDesktop()) return;
    } catch (e) {
      console.error("Quickscope: SwitchToDesktop failed", e);
    }
    await cancelPending();
    toast(`Couldn't switch to desktop: ${switched.error}`);
  }
}
