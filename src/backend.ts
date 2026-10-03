import { callable } from "@decky/api";

export interface Settings {
  return_to_gaming: boolean;
  force_fullscreen: boolean;
  power_profile: PowerProfile;
  lock_brightness: boolean;
  match_gaming_brightness: boolean;
  brightness_pct: number;
  /** "" matches Gaming Mode; otherwise e.g. "1920x1080@60.00". */
  display_mode: string;
  /** HDR with a forced display mode. */
  display_hdr: boolean;
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
}

export interface LaunchSpec {
  appid: number;
  name: string;
  kind: "steam" | "shortcut";
  exe?: string;
  start_dir?: string;
  launch_options?: string;
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
export const prepareLaunch = callable<[spec: LaunchSpec], Result>("prepare_launch");
export const switchSession = callable<[], Result>("switch_session");
export const cancelPending = callable<[], void>("cancel_pending");
