import {
  ButtonItem,
  DialogButton,
  DropdownItem,
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
  DisplayInfo,
  Environment,
  PowerProfile,
  Settings,
  cancelPending,
  getDisplays,
  getEnvironment,
  getSettings,
  setSetting,
} from "./backend";
import { patchGamePage } from "./gamepage";
import { addLaunchingRoute } from "./launching";
import { MODE_NAMES, effectiveMode, launch, toast } from "./launch";
import { LibraryApp, getLibraryApps, getSteamBrightness } from "./library";

const POWER_PROFILES: { data: PowerProfile; label: string; description: string }[] = [
  {
    data: "auto",
    label: "Automatic",
    description: "Battery saver on battery, High performance when plugged in. Switches if you plug in or unplug.",
  },
  {
    data: "performance",
    label: "High performance",
    description: "GPU and CPU at full speed while the app runs.",
  },
  {
    data: "battery",
    label: "Battery saver",
    description: "Turns off CPU boost. Uses about 40% less chip power while streaming.",
  },
];

function modeLabel(mode: string): string {
  const m = mode.match(/^(\d+)x(\d+)@([\d.]+)$/);
  if (!m) return mode;
  const hz = Number(m[3]);
  return `${m[1]} × ${m[2]}, ${Number.isInteger(hz) ? hz : hz.toFixed(2)} Hz`;
}

/** Largest resolution first, then highest refresh rate. */
function sortModes(modes: string[]): string[] {
  const key = (mode: string) => {
    const [w, h, hz] = mode.split(/[x@]/).map(Number);
    return [w * h, hz];
  };
  return [...modes].sort((a, b) => {
    const [ka, kb] = [key(a), key(b)];
    return kb[0] - ka[0] || kb[1] - ka[1];
  });
}

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
  const [display, setDisplay] = useState<DisplayInfo | null>(null);

  useEffect(() => {
    getSettings().then(setSettings);
    getEnvironment().then(setEnv);
    getDisplays().then(setDisplay);
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
        <PanelSectionRow>
          <DropdownItem
            label="Power profile"
            description={POWER_PROFILES.find((p) => p.data === settings.power_profile)?.description}
            rgOptions={POWER_PROFILES.map(({ data, label }) => ({ data, label }))}
            selectedOption={settings.power_profile}
            onChange={(o) => update("power_profile", o.data)}
          />
        </PanelSectionRow>
      </PanelSection>

      {display && (display.external || display.modes.length > 1) && (
        <PanelSection title={display.external ? "External display" : "Display"}>
          <PanelSectionRow>
            <ToggleField
              label="Match Gaming Mode"
              description={
                display.external
                  ? "Use the resolution and refresh rate Gaming Mode uses on this display. The Deck's screen stays off, as in Gaming Mode."
                  : "Use the refresh rate Gaming Mode uses."
              }
              checked={!settings.display_mode}
              onChange={(v) =>
                update("display_mode", v ? "" : display.current ?? display.modes[0])
              }
            />
          </PanelSectionRow>
          {!!settings.display_mode && (
            <PanelSectionRow>
              <DropdownItem
                label="Display mode"
                description={
                  display.modes.includes(settings.display_mode)
                    ? undefined
                    : "This display doesn't offer the saved mode, so Gaming Mode's is used."
                }
                rgOptions={sortModes(display.modes).map((m) => ({ data: m, label: modeLabel(m) }))}
                selectedOption={settings.display_mode}
                onChange={(o) => update("display_mode", o.data)}
              />
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {/* The Deck's own screen is off on an external display. */}
      {!display?.external && (
        <PanelSection title="Brightness">
          <PanelSectionRow>
            <ToggleField
              label="Match Gaming Mode brightness"
              description="Start the app at the brightness you had when launching."
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
          {!settings.match_gaming_brightness && (
            <PanelSectionRow>
              <SliderField
                label="Starting brightness"
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
              label="Lock brightness"
              description="Stop the desktop's power management from dimming or changing the screen. To change the brightness while an app runs, hold Steam and push the left stick up or down."
              checked={settings.lock_brightness}
              onChange={(v) => update("lock_brightness", v)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}
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
