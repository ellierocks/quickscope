import { callable } from "@decky/api";

export interface Settings {
  return_to_gaming: boolean;
  force_fullscreen: boolean;
  skip_splash: boolean;
  minimal_desktop: boolean;
  loading_screen: boolean;
  performance: boolean;
  lock_brightness: boolean;
  match_gaming_brightness: boolean;
  brightness_pct: number;
  volume_osd: boolean;
  favorites: number[];
  launch_modes: Record<string, ShortcutMode>;
}

/** How a launch actually runs. "steam" is only used for Steam games. */
export type LaunchMode = "steam" | "hybrid" | "direct";
/** Per-shortcut choice; shortcuts default to "hybrid". */
export type ShortcutMode = "hybrid" | "direct";

export interface Environment {
  launcher_found: boolean;
  pending: boolean;
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
export const prepareLaunch = callable<[spec: LaunchSpec], Result>("prepare_launch");
export const switchSession = callable<[], Result>("switch_session");
export const cancelPending = callable<[], void>("cancel_pending");
