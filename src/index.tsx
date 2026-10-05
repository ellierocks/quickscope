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
  getMoonlightHosts,
  getSettings,
  refreshMoonlightHosts,
  MoonlightApp,
  MoonlightHost,
  saveDiagnostics,
  setSetting,
} from "./backend";
import { patchGamePage } from "./gamepage";
import { addLaunchingRoute } from "./launching";
import { addMoonlightRoute, openMoonlightPage } from "./moonlight";
import { MODE_NAMES, effectiveMode, launch, launchStream, toast } from "./launch";
import { LibraryApp, getLibraryApps, getShortcutDetails, getSteamBrightness } from "./library";

const POWER_PROFILES: { data: PowerProfile; label: string; description: string }[] = [
  {
    data: "auto",
    label: "Automatic",
    description: "Battery saver on battery, High performance when plugged in.",
  },
  {
    data: "performance",
    label: "High performance",
    description: "GPU and CPU at full speed.",
  },
  {
    data: "battery",
    label: "Battery saver",
    description: "CPU boost off: about 40% less power while streaming.",
  },
];

// KDE's idle timers, in minutes. -1 leaves SteamOS's own settings.
const IDLE_MINUTES = [1, 2, 5, 10, 15, 30];
const idleOptions = (steamosDefault: string) => [
  { data: -1, label: `SteamOS default (${steamosDefault})` },
  { data: 0, label: "Never" },
  ...IDLE_MINUTES.map((m) => ({ data: m, label: m === 1 ? "1 minute" : `${m} minutes` })),
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
        onOptionsActionDescription={
          isShortcut ? (label === MODE_NAMES.direct ? "Use Hybrid" : "Use Direct") : undefined
        }
        style={{ padding: "8px 12px", minWidth: 0, borderRadius: 0 }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span
            style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {pinned ? "★ " : ""}
            {app.name}
          </span>
          <span style={{ fontSize: "0.7em", opacity: 0.6, whiteSpace: "nowrap" }}>{label}</span>
        </div>
      </DialogButton>
    </PanelSectionRow>
  );
}

/** A direct-to-stream entry: a host app, launched straight into the stream. */
function StreamRow({
  host,
  app,
  pinned,
  label,
  disabled,
  onLaunch,
  onTogglePin,
}: {
  host: MoonlightHost;
  app: MoonlightApp;
  pinned: boolean;
  /** Shown on the right, e.g. the host's name in the Pinned section. */
  label?: string;
  disabled: boolean;
  onLaunch: (host: MoonlightHost, app: MoonlightApp) => void;
  onTogglePin: (host: MoonlightHost, app: MoonlightApp) => void;
}) {
  return (
    <PanelSectionRow>
      <DialogButton
        disabled={disabled}
        onClick={() => onLaunch(host, app)}
        onSecondaryButton={() => onTogglePin(host, app)}
        onSecondaryActionDescription={pinned ? "Unpin" : "Pin"}
        style={{ padding: "8px 12px", minWidth: 0, borderRadius: 0 }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span
            style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {pinned ? "★ " : ""}
            {app.name.trim()}
          </span>
          {label && <span style={{ fontSize: "0.7em", opacity: 0.6, whiteSpace: "nowrap" }}>{label}</span>}
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
  // null until loaded, so the "no hosts yet" hint doesn't flash up.
  const [hostList, setHosts] = useState<MoonlightHost[] | null>(null);
  const hosts = hostList ?? [];

  useEffect(() => {
    getSettings().then(setSettings);
    getEnvironment().then(setEnv);
    getDisplays().then(setDisplay);
    getMoonlightHosts().then(setHosts);
  }, []);

  // Quickscope is built for Moonlight: its shortcut(s) lead the panel.
  const moonlight = useMemo(() => apps.filter((a) => a.kind === "shortcut" && /moonlight/i.test(a.name)), [apps]);

  // Moonlight.conf only has the apps Moonlight's own window last saw, so ask
  // the hosts for their current ones each time the panel opens.
  useEffect(() => {
    let open = true;
    (async () => {
      const details = moonlight[0] && (await getShortcutDetails(moonlight[0].appid));
      const fresh = await refreshMoonlightHosts(details ? details.exe : null);
      if (open) setHosts(fresh);
    })().catch((e) => console.error("Quickscope: refreshing Moonlight hosts failed", e));
    return () => {
      open = false;
    };
  }, [moonlight]);

  const pinned = useMemo(() => {
    const ids = settings?.favorites ?? [];
    return ids
      .map((id) => apps.find((a) => a.appid === id))
      .filter((a): a is LibraryApp => !!a && !moonlight.includes(a));
  }, [apps, moonlight, settings?.favorites]);

  // Everything else (other shortcuts, Steam games) only through search.
  const listed = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? apps.filter((a) => a.name.toLowerCase().includes(q)).slice(0, SEARCH_COUNT) : [];
  }, [apps, filter]);

  if (!settings) return null;

  // Nothing to switch off if power saving is already off everywhere.
  const showPowersave = env?.wifi_powersave !== "disabled";
  // Only wpa_supplicant scans and roams in the background.
  const showLockAp = env?.wifi_backend === "wpa_supplicant";

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

  const onStream = async (host: MoonlightHost, app: MoonlightApp) => {
    setBusy(true);
    try {
      await launchStream(host, app, moonlight[0]);
    } catch (e) {
      toast(`Launch failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const isPinnedStream = (host: MoonlightHost, app: MoonlightApp) =>
    settings.pinned_streams.some((p) => p.host === host.name && p.app === app.name);

  const onToggleStreamPin = (host: MoonlightHost, app: MoonlightApp) => {
    const pins = settings.pinned_streams;
    update(
      "pinned_streams",
      isPinnedStream(host, app)
        ? pins.filter((p) => !(p.host === host.name && p.app === app.name))
        : [...pins, { host: host.name, app: app.name }],
    );
  };

  // Pinned streams that the host still offers, in pin order.
  const pinnedStreams = settings.pinned_streams.flatMap((p) => {
    const host = hosts.find((h) => h.name === p.host);
    const app = host?.apps.find((a) => a.name === p.app);
    return host && app ? [{ host, app }] : [];
  });

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
    toast(direct ? `${app.name}: Direct, without Steam Input.` : `${app.name}: Hybrid, with Steam's desktop layout.`);
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

      {(pinnedStreams.length > 0 || pinned.length > 0) && (
        <PanelSection title="Pinned">
          {pinnedStreams.map(({ host, app }) => (
            <StreamRow
              key={`${host.name}-${app.name}`}
              host={host}
              app={app}
              pinned
              label={hosts.length > 1 ? host.name : undefined}
              disabled={busy}
              onLaunch={onStream}
              onTogglePin={onToggleStreamPin}
            />
          ))}
          {pinned.map(row)}
        </PanelSection>
      )}

      {/* First run: say what's missing instead of showing nothing. */}
      {env && hostList && !hosts.some((h) => h.apps.length > 0) && (
        <PanelSection title="Stream">
          <PanelSectionRow>
            <div style={{ opacity: 0.6, fontSize: "0.85em" }}>
              {env.moonlight_installed || moonlight.length > 0
                ? "Pair Moonlight with your PC to see its apps here."
                : "Install Moonlight from Discover and pair it with your PC to see its apps here."}
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Moonlight's saved hosts: straight into a host app, skipping Moonlight's menus. */}
      {hosts
        .filter((h) => h.apps.length > 0)
        .map((host) => (
          <PanelSection key={host.uuid || host.name} title={`Stream from ${host.name}`}>
            {host.apps.map((app) => (
              <StreamRow
                key={`${app.id}-${app.name}`}
                host={host}
                app={app}
                pinned={isPinnedStream(host, app)}
                disabled={busy}
                onLaunch={onStream}
                onTogglePin={onToggleStreamPin}
              />
            ))}
          </PanelSection>
        ))}

      {/* Moonlight itself, for its menus (hosts, settings, pairing). */}
      <PanelSection title="Moonlight">
        {moonlight.map(row)}
        {moonlight.length === 0 && (
          <PanelSectionRow>
            <div style={{ opacity: 0.6, fontSize: "0.85em" }}>
              {env?.moonlight_installed
                ? "Add Moonlight as a non-Steam game to open it here."
                : "Add Moonlight as a non-Steam game to launch it here."}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Other apps">
        <PanelSectionRow>
          <TextField label="Search" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </PanelSectionRow>
        {listed.map(row)}
        {filter.trim() && listed.length === 0 && (
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
            checked={settings.force_fullscreen}
            onChange={(v) => update("force_fullscreen", v)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Reconnect dropped streams"
            description="Start a dropped stream again, e.g. after sleep."
            checked={settings.reconnect_streams}
            onChange={(v) => update("reconnect_streams", v)}
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
        <PanelSectionRow>
          <DropdownItem
            label="Sleep when idle"
            description="Moonlight keeps the Deck awake while streaming."
            rgOptions={idleOptions("5 minutes on battery")}
            selectedOption={settings.idle_sleep_min}
            onChange={(o) => update("idle_sleep_min", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Turn off screen when idle"
            rgOptions={idleOptions("1 minute on battery")}
            selectedOption={settings.idle_screen_off_min}
            onChange={(o) => update("idle_screen_off_min", o.data)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description={settings.moonlight_override ? "Overriding Moonlight's settings per display." : undefined}
            onClick={openMoonlightPage}
          >
            Moonlight settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {(showPowersave || showLockAp) && (
        <PanelSection title="Network">
          {showPowersave && (
            <PanelSectionRow>
              <ToggleField
                label="Disable Wi-Fi power saving"
                description="Avoids latency spikes."
                checked={settings.wifi_powersave_off}
                onChange={(v) => update("wifi_powersave_off", v)}
              />
            </PanelSectionRow>
          )}
          {showLockAp && (
            <PanelSectionRow>
              <ToggleField
                label="Lock Wi-Fi access point"
                description="No roaming or scans while streaming."
                checked={settings.wifi_lock_ap}
                onChange={(v) => update("wifi_lock_ap", v)}
              />
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {display && (
        <PanelSection title={display.external ? "External display" : "Display"}>
          {/* A single-mode SDR panel like the Deck LCD has nothing to choose. */}
          {(display.external || display.modes.length > 1 || display.hdr_capable) && (
            <PanelSectionRow>
              <ToggleField
                label="Match Gaming Mode"
                description={
                  display.external
                    ? "Resolution, refresh rate and HDR as in Gaming Mode. The Deck's screen stays off."
                    : "Refresh rate and HDR as in Gaming Mode."
                }
                checked={!settings.display_mode}
                onChange={(v) => update("display_mode", v ? "" : (display.current ?? display.modes[0]))}
              />
            </PanelSectionRow>
          )}
          {!!settings.display_mode && (
            <PanelSectionRow>
              <DropdownItem
                label="Display mode"
                description={
                  display.modes.includes(settings.display_mode)
                    ? undefined
                    : "Not offered by this display, so Gaming Mode's is used."
                }
                rgOptions={sortModes(display.modes).map((m) => ({ data: m, label: modeLabel(m) }))}
                selectedOption={settings.display_mode}
                onChange={(o) => update("display_mode", o.data)}
              />
            </PanelSectionRow>
          )}
          {!!settings.display_mode && display.hdr_capable && (
            <PanelSectionRow>
              <ToggleField label="HDR" checked={settings.display_hdr} onChange={(v) => update("display_hdr", v)} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <SliderField
              label="Scale"
              description="Size of menus and text. Streams stay at full resolution."
              value={settings.display_scale}
              min={100}
              max={300}
              step={25}
              showValue
              valueSuffix="%"
              onChange={(v) => update("display_scale", v)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* The Deck's own screen is off on an external display. */}
      {!display?.external && (
        <PanelSection title="Brightness">
          <PanelSectionRow>
            <ToggleField
              label="Match Gaming Mode brightness"
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
              description="Stops KDE changing it. Hold … and push the left stick to adjust."
              checked={settings.lock_brightness}
              onChange={(v) => update("lock_brightness", v)}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Help">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="Saves a report to Downloads, with network details masked."
            onClick={async () => {
              try {
                toast(`Saved ${await saveDiagnostics()}`);
              } catch (e) {
                toast(`Couldn't save diagnostics: ${e}`);
              }
            }}
          >
            Save diagnostics
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const unpatchGamePage = patchGamePage();
  const removeLaunchingRoute = addLaunchingRoute();
  const removeMoonlightRoute = addMoonlightRoute();
  return {
    name: "Quickscope",
    titleView: <div className={staticClasses.Title}>Quickscope</div>,
    content: <Content />,
    icon: <FaCrosshairs />,
    onDismount() {
      unpatchGamePage();
      removeLaunchingRoute();
      removeMoonlightRoute();
    },
  };
});
