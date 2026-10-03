// Full-screen page for the opt-in Moonlight settings override: one profile
// per display, picked automatically by the display the app uses.
import { routerHook } from "@decky/api";
import {
  ButtonItem,
  DialogBody,
  DialogControlsSection,
  DialogControlsSectionHeader,
  DropdownItem,
  Navigation,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { useEffect, useState } from "react";

import {
  DisplayInfo,
  MoonlightProfile,
  MoonlightSettings,
  Settings,
  getDisplays,
  getMoonlightSettings,
  getSettings,
  setSetting,
} from "./backend";

export const MOONLIGHT_ROUTE = "/quickscope/moonlight";

// Higher than the screen is fine: Moonlight downscales, which supersamples.
const RESOLUTIONS: [number, number][] = [
  [1280, 720], [1280, 800], [1600, 900], [1920, 1080], [1920, 1200],
  [2560, 1440], [2560, 1600], [3840, 2160], [3840, 2400],
];
const FRAME_RATES = [30, 40, 45, 50, 60, 72, 90, 120];

// Space between a section and the one above it.
const SECTION_GAP = { marginTop: "32px" };

/** The display's own resolution and refresh rate, landscape (the Deck LCD reports 800x1280). */
function nativeMode(display: DisplayInfo | null): { width: number; height: number; fps: number } | null {
  const mode = display?.modes[0]?.match(/^(\d+)x(\d+)@([\d.]+)$/);
  if (!mode) return null;
  const [a, b] = [Number(mode[1]), Number(mode[2])];
  return { width: Math.max(a, b), height: Math.min(a, b), fps: Math.round(Number(mode[3])) };
}

const num = (v: string | null | undefined, fallback: number) => (v && /^\d+$/.test(v) ? Number(v) : fallback);
const bool = (v: string | null | undefined, fallback: boolean) => (v ? v === "true" : fallback);

// Moonlight's own default for 1080p60.
const DEFAULT_BITRATE = 20000;

function profileFrom(display: DisplayInfo, current: MoonlightSettings | null): MoonlightProfile {
  const native = nativeMode(display);
  return {
    name: display.name,
    width: num(current?.width, native?.width ?? 1280),
    height: num(current?.height, native?.height ?? 800),
    fps: num(current?.fps, native?.fps ?? 60),
    bitrate: num(current?.bitrate, DEFAULT_BITRATE),
    vsync: bool(current?.vsync, true),
    framepacing: bool(current?.framepacing, true),
    hdr: bool(current?.hdr, false),
    codec: num(current?.codec, 0),
    vrr_capable: display.vrr_capable,
    // Only Nonary's fork knows VRR; upstream profiles leave it out entirely.
    ...(current?.vrr_fork ? { vrr: display.vrr_capable && bool(current.vrr, false) } : {}),
  };
}

const CODECS = [
  { data: 0, label: "Automatic" },
  { data: 1, label: "H.264" },
  { data: 2, label: "HEVC" },
  { data: 4, label: "AV1" },
];
const PYROWAVE = { data: 5, label: "PyroWave" };

/** Nonary's fork's VRR stream rates for a refresh rate (its VrrRatePolicy). */
function vrrRates(refresh: number): { vrr: number; lowLatency: number } {
  return { vrr: Math.floor((refresh * (3600 - refresh)) / 3600), lowLatency: Math.floor(refresh / 6) * 5 };
}

function ProfileEditor({
  profile,
  native,
  current,
  vrrCapable,
  onChange,
}: {
  profile: MoonlightProfile;
  native: { width: number; height: number; fps: number } | null;
  /** Moonlight's own values, shown for fields an older profile doesn't have. */
  current: MoonlightSettings | null;
  /** Whether to offer VRR: Nonary's fork, on a display that may support it. */
  vrrCapable: boolean;
  onChange: (profile: MoonlightProfile) => void;
}) {
  const fork = !!current?.vrr_fork;
  const bitrate = profile.bitrate ?? num(current?.bitrate, DEFAULT_BITRATE);
  const hdr = profile.hdr ?? bool(current?.hdr, false);
  const codecs = fork ? [...CODECS, PYROWAVE] : CODECS;
  let codec = profile.codec ?? num(current?.codec, 0);
  // PyroWave saved by the fork, now running upstream Moonlight.
  if (!codecs.some((c) => c.data === codec)) codec = 0;
  const vrr = fork && vrrCapable && (profile.vrr ?? bool(current?.vrr, false));
  const vrrChoices = vrr && native ? vrrRates(native.fps) : null;
  const resolutions = [...RESOLUTIONS];
  const extra: [number, number][] = [[profile.width, profile.height]];
  if (native) extra.push([native.width, native.height]);
  for (const [w, h] of extra) {
    if (!resolutions.some(([rw, rh]) => rw === w && rh === h)) resolutions.push([w, h]);
  }
  resolutions.sort(([aw, ah], [bw, bh]) => aw * ah - bw * bh);
  const rates = [
    ...new Set([
      ...FRAME_RATES,
      profile.fps,
      ...(native ? [native.fps] : []),
      ...(vrrChoices ? [vrrChoices.vrr, vrrChoices.lowLatency] : []),
    ]),
  ].sort((a, b) => a - b);
  const rateLabel = (fps: number) =>
    `${fps} FPS` +
    (vrrChoices?.vrr === fps
      ? " (VRR)"
      : vrrChoices?.lowLatency === fps
        ? " (low-latency VRR)"
        : native?.fps === fps
          ? " (screen)"
          : "");
  const isNative = (w: number, h: number) => native?.width === w && native?.height === h;

  return (
    <>
      <DropdownItem
        label="Resolution"
        description="Higher than the screen supersamples: sharper, but more bandwidth and decoding work."
        rgOptions={resolutions.map(([w, h]) => ({
          data: `${w}x${h}`,
          label: `${w} × ${h}${isNative(w, h) ? " (screen)" : ""}`,
        }))}
        selectedOption={`${profile.width}x${profile.height}`}
        onChange={(o) => {
          const [width, height] = String(o.data).split("x").map(Number);
          onChange({ ...profile, width, height });
        }}
      />
      {fork && vrrCapable && (
        <ToggleField
          label="VRR"
          description="Variable refresh rate (Nonary's Moonlight fork). Needs V-Sync, and adds VRR frame rates below."
          checked={vrr}
          onChange={(on) =>
            onChange({
              ...profile,
              vrr: on,
              vsync: on || profile.vsync,
              // Start on the fork's recommended VRR rate.
              fps: on && native ? vrrRates(native.fps).vrr : profile.fps,
            })
          }
        />
      )}
      <DropdownItem
        label="Frame rate"
        rgOptions={rates.map((fps) => ({ data: fps, label: rateLabel(fps) }))}
        selectedOption={profile.fps}
        onChange={(o) => onChange({ ...profile, fps: Number(o.data) })}
      />
      <DropdownItem
        label="Video codec"
        rgOptions={codecs}
        selectedOption={codec}
        onChange={(o) => onChange({ ...profile, codec: Number(o.data) })}
      />
      <SliderField
        label="Bitrate"
        value={Math.round(bitrate / 1000)}
        min={5}
        max={500}
        step={5}
        showValue
        valueSuffix=" Mbps"
        onChange={(mbps) => onChange({ ...profile, bitrate: mbps * 1000 })}
      />
      <ToggleField
        label="V-Sync"
        description={vrr ? "Always on with VRR." : undefined}
        disabled={vrr}
        checked={profile.vsync || vrr}
        onChange={(vsync) => onChange({ ...profile, vsync })}
      />
      <ToggleField
        label="Frame pacing"
        checked={profile.framepacing}
        onChange={(framepacing) => onChange({ ...profile, framepacing })}
      />
      <ToggleField
        label="HDR"
        description="Streams in HDR when the host and display support it. For the display itself, see Quickscope's display settings."
        checked={hdr}
        onChange={(on) => onChange({ ...profile, hdr: on })}
      />
    </>
  );
}

function MoonlightPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [display, setDisplay] = useState<DisplayInfo | null>(null);
  const [current, setCurrent] = useState<MoonlightSettings | null>(null);

  useEffect(() => {
    getSettings().then(setSettings);
    getDisplays().then(setDisplay);
    getMoonlightSettings().then(setCurrent);
  }, []);

  if (!settings) return null;

  const profiles = settings.moonlight_profiles;
  const save = async (next: Record<string, MoonlightProfile>) => {
    setSettings({ ...settings, moonlight_profiles: next });
    setSettings(await setSetting("moonlight_profiles", next));
  };
  const setOverride = async (on: boolean) => {
    // Start this display's profile from Moonlight's own settings, so turning
    // the override on changes nothing until a value is edited.
    if (on && display && !profiles[display.id]) await save({ ...profiles, [display.id]: profileFrom(display, current) });
    setSettings(await setSetting("moonlight_override", on));
  };

  const others = Object.keys(profiles).filter((id) => id !== display?.id);

  return (
    // Scrolls between Steam's top bar and its button bar (40px each); anything
    // under the button bar would count as on screen and never scroll into view.
    <div style={{ marginTop: "40px", height: "calc(100% - 80px)", overflowY: "auto" }}>
      {/* Inset and centred like Steam's own settings pages. DialogBody scrolls
          by itself too; only the outer box should. */}
      <DialogBody style={{ maxWidth: "900px", margin: "0 auto", padding: "16px 2.8vw 24px", overflow: "visible" }}>
        <DialogControlsSection>
          <DialogControlsSectionHeader>Moonlight</DialogControlsSectionHeader>
          <ToggleField
            label="Override Moonlight settings"
            description="Use the settings below for the display Moonlight runs on, instead of Moonlight's own. Your Moonlight settings are put back when you return; anything you change in Moonlight meanwhile is kept."
            checked={settings.moonlight_override}
            onChange={setOverride}
          />
        </DialogControlsSection>

        {settings.moonlight_override && display && (
          <DialogControlsSection style={SECTION_GAP}>
            <DialogControlsSectionHeader>{display.name} (this display)</DialogControlsSectionHeader>
            {profiles[display.id] ? (
              <ProfileEditor
                profile={profiles[display.id]}
                native={nativeMode(display)}
                current={current}
                vrrCapable={display.vrr_capable}
                onChange={(p) => save({ ...profiles, [display.id]: { ...p, vrr_capable: display.vrr_capable } })}
              />
            ) : (
              <ButtonItem
                layout="below"
                description="Moonlight uses its own settings on this display until you add some here."
                onClick={() => save({ ...profiles, [display.id]: profileFrom(display, current) })}
              >
                Add settings for {display.name}
              </ButtonItem>
            )}
          </DialogControlsSection>
        )}

        {settings.moonlight_override &&
          others.map((id) => (
            <DialogControlsSection key={id} style={SECTION_GAP}>
              <DialogControlsSectionHeader>{profiles[id].name || id}</DialogControlsSectionHeader>
              <ProfileEditor
                profile={profiles[id]}
                native={null}
                current={current}
                vrrCapable={!!profiles[id].vrr_capable}
                onChange={(p) => save({ ...profiles, [id]: p })}
              />
              <ButtonItem
                layout="below"
                onClick={() => {
                  const { [id]: _removed, ...rest } = profiles;
                  save(rest);
                }}
              >
                Forget {profiles[id].name || id}
              </ButtonItem>
            </DialogControlsSection>
          ))}
      </DialogBody>
    </div>
  );
}

export function openMoonlightPage() {
  Navigation.CloseSideMenus();
  Navigation.Navigate(MOONLIGHT_ROUTE);
}

/** Register the page's route. Returns a function that removes it. */
export function addMoonlightRoute(): () => void {
  routerHook.addRoute(MOONLIGHT_ROUTE, MoonlightPage, { exact: true });
  return () => routerHook.removeRoute(MOONLIGHT_ROUTE);
}
