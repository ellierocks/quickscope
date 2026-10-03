import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { definePlugin, toaster } from "@decky/api";
import { useEffect, useMemo, useState } from "react";
import { FaCrosshairs } from "react-icons/fa";

import {
  DesktopSession,
  Environment,
  LaunchMode,
  LaunchSpec,
  Settings,
  ShortcutMode,
  cancelPending,
  getEnvironment,
  getSettings,
  prepareLaunch,
  setSetting,
  switchSession,
} from "./backend";
import { LibraryApp, getLibraryApps, getShortcutDetails, steamSwitchToDesktop } from "./library";

const RECENT_COUNT = 8;
const SEARCH_COUNT = 20;

const SESSION_OPTIONS: { data: DesktopSession; label: string }[] = [
  { data: "auto", label: "System default" },
  { data: "plasma", label: "Plasma X11" },
  { data: "plasma-wayland", label: "Plasma Wayland" },
];

function toast(body: string) {
  toaster.toast({ title: "Quickscope", body });
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

const DEFAULT_SHORTCUT_MODE: ShortcutMode = "hybrid";

const MODE_NAMES: Record<LaunchMode, string> = {
  steam: "Steam",
  hybrid: "Hybrid",
  direct: "Direct",
};

function effectiveMode(app: LibraryApp, settings: Settings): LaunchMode {
  // Steam games can only be started by Steam.
  if (app.kind === "steam") return "steam";
  return settings.launch_modes[app.appid] ?? DEFAULT_SHORTCUT_MODE;
}

async function launch(app: LibraryApp, settings: Settings) {
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

  const switched = await switchSession(settings.desktop_session);
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

function AppRow({
  app,
  pinned,
  label,
  disabled,
  onLaunch,
  onTogglePin,
  onCycleMode,
}: {
  app: LibraryApp;
  pinned: boolean;
  label: string;
  disabled: boolean;
  onLaunch: (app: LibraryApp) => void;
  onTogglePin: (app: LibraryApp) => void;
  onCycleMode: (app: LibraryApp) => void;
}) {
  const isShortcut = app.kind === "shortcut";
  return (
    <PanelSectionRow>
      <DialogButton
        disabled={disabled}
        onClick={() => onLaunch(app)}
        onSecondaryButton={() => onTogglePin(app)}
        onSecondaryActionDescription={pinned ? "Unpin" : "Pin"}
        onOptionsButton={isShortcut ? () => onCycleMode(app) : undefined}
        onOptionsActionDescription={isShortcut ? (label === MODE_NAMES.direct ? "Use Hybrid" : "Use Direct") : undefined}
        style={{ padding: "8px 12px", minWidth: 0 }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {pinned ? "★ " : ""}
            {app.name}
          </span>
          <span style={{ fontSize: "0.7em", opacity: 0.6, whiteSpace: "nowrap" }}>{label}</span>
        </div>
      </DialogButton>
    </PanelSectionRow>
  );
}

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [env, setEnv] = useState<Environment | null>(null);
  const [apps] = useState<LibraryApp[]>(() => getLibraryApps());
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSettings().then(setSettings);
    getEnvironment().then(setEnv);
  }, []);

  const pinned = useMemo(() => {
    const ids = settings?.favorites ?? [];
    return ids.map((id) => apps.find((a) => a.appid === id)).filter((a): a is LibraryApp => !!a);
  }, [apps, settings?.favorites]);

  const listed = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return apps.slice(0, RECENT_COUNT);
    return apps.filter((a) => a.name.toLowerCase().includes(q)).slice(0, SEARCH_COUNT);
  }, [apps, filter]);

  if (!settings) return null;

  const update = async <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setSettings((s) => s && { ...s, [key]: value });
    setSettings(await setSetting(key, value));
  };

  const onLaunch = async (app: LibraryApp) => {
    setBusy(true);
    try {
      await launch(app, settings);
    } catch (e) {
      toast(`Launch failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const onTogglePin = (app: LibraryApp) => {
    const favs = settings.favorites;
    update("favorites", favs.includes(app.appid) ? favs.filter((id) => id !== app.appid) : [...favs, app.appid]);
  };

  // Y toggles between the default (Hybrid) and the Direct fallback.
  const onCycleMode = (app: LibraryApp) => {
    const modes = { ...settings.launch_modes };
    const direct = modes[app.appid] !== "direct";
    if (direct) modes[app.appid] = "direct";
    else delete modes[app.appid];
    update("launch_modes", modes);
    toast(
      direct
        ? `${app.name}: Direct. No Steam, so no Steam Input layout.`
        : `${app.name}: Hybrid. Starts right away; Steam takes over the controller with your desktop layout.`,
    );
  };

  const row = (app: LibraryApp) => (
    <AppRow
      key={app.appid}
      app={app}
      pinned={settings.favorites.includes(app.appid)}
      label={MODE_NAMES[effectiveMode(app, settings)]}
      disabled={busy}
      onLaunch={onLaunch}
      onTogglePin={onTogglePin}
      onCycleMode={onCycleMode}
    />
  );

  return (
    <>
      {env?.pending && (
        <PanelSection title="Pending launch">
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              description="A launch is staged for the next desktop login."
              onClick={async () => {
                await cancelPending();
                setEnv({ ...env, pending: false });
              }}
            >
              Cancel pending launch
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {env && !env.launcher_found && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ color: "#ff7b7b", fontSize: "0.85em" }}>
              quickscope_launcher.py is missing from the plugin install. Reinstall Quickscope.
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {pinned.length > 0 && <PanelSection title="Pinned">{pinned.map(row)}</PanelSection>}

      <PanelSection title={filter.trim() ? "Search" : "Recent"}>
        <PanelSectionRow>
          <TextField value={filter} onChange={(e) => setFilter(e.target.value)} />
        </PanelSectionRow>
        {listed.map(row)}
        {listed.length === 0 && (
          <PanelSectionRow>
            <div style={{ opacity: 0.6, fontSize: "0.85em" }}>No matching apps.</div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <div style={{ opacity: 0.5, fontSize: "0.75em" }}>A: launch outside Gamescope · X: pin · Y: Hybrid / Direct (non-Steam)</div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <ToggleField
            label="Return to Gaming Mode on exit"
            checked={settings.return_to_gaming}
            onChange={(v) => update("return_to_gaming", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Force fullscreen"
            description="Fullscreen the app's first window as soon as it opens."
            checked={settings.force_fullscreen}
            onChange={(v) => update("force_fullscreen", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Suspend desktop compositor"
            description="X11 sessions only. Wayland already uses direct scanout for fullscreen."
            checked={settings.suspend_compositor}
            onChange={(v) => update("suspend_compositor", v)}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Faster startup">
        <PanelSectionRow>
          <ToggleField
            label="Loading screen"
            description="Show a loading screen instead of a black screen until the app's window appears."
            checked={settings.loading_screen}
            onChange={(v) => update("loading_screen", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Skip splash screen"
            checked={settings.skip_splash}
            onChange={(v) => update("skip_splash", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Minimal desktop"
            description="Skip the Plasma panel and desktop entirely. Only KWin and your app run."
            checked={settings.minimal_desktop}
            onChange={(v) => update("minimal_desktop", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Desktop session"
            description={
              env && !env.session_select
                ? "steamosctl not found; falling back to Steam's Switch to Desktop."
                : "System default follows SteamOS's default desktop session."
            }
            rgOptions={SESSION_OPTIONS}
            selectedOption={settings.desktop_session}
            onChange={(o) => update("desktop_session", o.data as DesktopSession)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Extra launch delay"
            description="Seconds to wait after KWin is ready. Leave at 0 unless something pops over your app."
            value={settings.launch_delay}
            min={0}
            max={15}
            step={1}
            showValue
            onChange={(v) => update("launch_delay", v)}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Quickscope",
  titleView: <div className={staticClasses.Title}>Quickscope</div>,
  content: <Content />,
  icon: <FaCrosshairs />,
}));
