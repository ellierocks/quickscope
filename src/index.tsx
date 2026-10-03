import {
  ButtonItem,
  DialogButton,
  PanelSection,
  PanelSectionRow,
  SliderField,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { definePlugin } from "@decky/api";
import { useEffect, useMemo, useState } from "react";
import { FaCrosshairs } from "react-icons/fa";

import {
  Environment,
  Settings,
  cancelPending,
  getEnvironment,
  getSettings,
  setSetting,
} from "./backend";
import { patchGamePage } from "./gamepage";
import { addLaunchingRoute } from "./launching";
import { MODE_NAMES, effectiveMode, launch, toast } from "./launch";
import { LibraryApp, getLibraryApps, getSteamBrightness } from "./library";

const RECENT_COUNT = 8;
const SEARCH_COUNT = 20;

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
        style={{ padding: "8px 12px", minWidth: 0, borderRadius: 0 }}
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
      await launch(app);
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
          <ToggleField
            label="Performance mode"
            description="Keep GPU and CPU at full speed while the app runs. Uses more battery."
            checked={settings.performance}
            onChange={(v) => update("performance", v)}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Display & audio">
        <PanelSectionRow>
          <ToggleField
            label="Lock brightness"
            description="Hold the screen at one brightness while the app runs; there's no Quick Access menu to change it."
            checked={settings.lock_brightness}
            onChange={(v) => update("lock_brightness", v)}
          />
        </PanelSectionRow>
        {settings.lock_brightness && (
          <PanelSectionRow>
            <ToggleField
              label="Match Gaming Mode"
              description="Use the brightness you had when launching."
              checked={settings.match_gaming_brightness}
              onChange={async (v) => {
                // Start the slider at the current brightness.
                if (!v) {
                  const current = await getSteamBrightness();
                  if (current !== null) await update("brightness_pct", Math.max(5, current));
                }
                update("match_gaming_brightness", v);
              }}
            />
          </PanelSectionRow>
        )}
        {settings.lock_brightness && !settings.match_gaming_brightness && (
          <PanelSectionRow>
            <SliderField
              label="Brightness"
              value={settings.brightness_pct}
              min={5}
              max={100}
              step={5}
              showValue
              onChange={(v) => update("brightness_pct", v)}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Volume indicator"
            description="Show the volume when you press the volume buttons. Plasma's panel usually does this, but the minimal desktop skips it."
            checked={settings.volume_osd}
            onChange={(v) => update("volume_osd", v)}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const unpatchGamePage = patchGamePage();
  const removeLaunchingRoute = addLaunchingRoute();
  return {
    name: "Quickscope",
    titleView: <div className={staticClasses.Title}>Quickscope</div>,
    content: <Content />,
    icon: <FaCrosshairs />,
    onDismount() {
      unpatchGamePage();
      removeLaunchingRoute();
    },
  };
});
