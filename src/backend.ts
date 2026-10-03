import { callable } from "@decky/api";

export interface Settings {
  return_to_gaming: boolean;
  force_fullscreen: boolean;
  power_profile: PowerProfile;
  wifi_powersave_off: boolean;
  wifi_lock_ap: boolean;
  lock_brightness: boolean;
  match_gaming_brightness: boolean;
  brightness_pct: number;
  /** "" matches Gaming Mode; otherwise e.g. "1920x1080@60.00". */
  display_mode: string;
  /** HDR with a forced display mode. */
  display_hdr: boolean;
  /** Percent, 100-300. */
  display_scale: number;
  moonlight_override: boolean;
  /** Keyed by display id, e.g. "SAM-71B5". */
  moonlight_profiles: Record<string, MoonlightProfile>;
  favorites: number[];
  launch_modes: Record<string, ShortcutMode>;
}

export type PowerProfile = "auto" | "performance" | "battery";

/** How a launch actually runs. "steam" is only used for Steam games. */
export type LaunchMode = "steam" | "hybrid" | "direct";
/** Per-shortcut choice; shortcuts default to "hybrid". */
export type ShortcutMode = "hybrid" | "direct";

export interface Environment {
  launcher_found: boolean;
  pending: boolean;
  /** "iwd" (SteamOS's default) or "wpa_supplicant" (forced in Developer settings). */
  wifi_backend: string | null;
  /** Wi-Fi power management outside sessions: "enabled" or "disabled". */
  wifi_powersave: string | null;
}

export interface DisplayInfo {
  connector: string;
  external: boolean;
  others: string[];
  /** Mode names like "3840x2160@60.00", the display's preferred mode first. */
  modes: string[];
  /** Gamescope's current mode, if it's one of the display's own. */
  current: string | null;
  /** The display's EDID advertises HDR (PQ). */
  hdr_capable: boolean;
  /** Gamescope is outputting HDR. */
  hdr: boolean;
  /** The display (and its connection) supports variable refresh rate. */
  vrr_capable: boolean;
  /** Stable per display model, e.g. "SAM-71B5". */
  id: string;
  /** E.g. "SAMSUNG", or "Built-in screen". */
  name: string;
}

export interface MoonlightProfile {
  /** The display's name when the profile was made. */
  name: string;
  width: number;
  height: number;
  fps: number;
  vsync: boolean;
  framepacing: boolean;
  /** kbps. This and the fields below are missing in older profiles. */
  bitrate?: number;
  hdr?: boolean;
  /** Moonlight's videocfg: 0 auto, 1 H.264, 2 HEVC, 4 AV1, 5 PyroWave. */
  codec?: number;
  /** Nonary's VRR fork only. */
  vrr?: boolean;
  /** Whether the display supported VRR when the profile was made. */
  vrr_capable?: boolean;
}

/** Moonlight.conf's raw values, as strings ("1280", "true"), or null if unset. */
export type MoonlightSettings = Record<
  "width" | "height" | "fps" | "bitrate" | "vsync" | "framepacing" | "hdr" | "codec" | "vrr",
  string | null
> & {
  /** Nonary's VRR fork is installed (its settings have VRR keys). */
  vrr_fork: boolean;
};

export interface LaunchSpec {
  appid: number;
  name: string;
  kind: "steam" | "shortcut" | "stream";
  exe?: string;
  start_dir?: string;
  launch_options?: string;
  /** For "stream": Moonlight host name and app name (exact, spaces included). */
  host?: string;
  app?: string;
}

export interface MoonlightApp {
  id: number;
  /** Exactly as the host names it; Vibeshine pads some with leading spaces to order them. */
  name: string;
}

export interface MoonlightHost {
  name: string;
  uuid: string;
  apps: MoonlightApp[];
}

export interface Result {
  ok: boolean;
  error?: string;
  mode?: LaunchMode;
}

export const getSettings = callable<[], Settings>("get_settings");
export const setSetting = callable<[key: keyof Settings, value: unknown], Settings>("set_setting");
export const getEnvironment = callable<[], Environment>("get_environment");
export const getDisplays = callable<[], DisplayInfo | null>("get_displays");
export const getMoonlightSettings = callable<[], MoonlightSettings | null>("get_moonlight_settings");
export const saveDiagnostics = callable<[], string>("save_diagnostics");
export const getMoonlightHosts = callable<[], MoonlightHost[]>("get_moonlight_hosts");
export const prepareLaunch = callable<[spec: LaunchSpec], Result>("prepare_launch");
export const switchSession = callable<[], Result>("switch_session");
export const cancelPending = callable<[], void>("cancel_pending");
