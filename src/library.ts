// Thin wrappers over Steam's internal stores, which have no published types.

const SHORTCUT_APP_TYPE = 1073741824;
// Game, Application, Demo
const STEAM_APP_TYPES = new Set([1, 2, 8]);

export interface LibraryApp {
  appid: number;
  name: string;
  kind: "steam" | "shortcut";
  lastPlayed: number;
}

export interface ShortcutDetails {
  exe: string;
  startDir: string;
  launchOptions: string;
}

const w = window as any;

/** A library app from a Steam app overview, or null if it isn't a game, app or shortcut. */
export function toLibraryApp(o: any): LibraryApp | null {
  if (!o || typeof o.appid !== "number") return null;
  const isShortcut = o.app_type === SHORTCUT_APP_TYPE;
  if (!isShortcut && !STEAM_APP_TYPES.has(o.app_type)) return null;
  return {
    appid: o.appid,
    name: o.display_name ?? String(o.appid),
    kind: isShortcut ? "shortcut" : "steam",
    lastPlayed: o.rt_last_time_played ?? 0,
  };
}

/** Whether Quickscope can launch this overview: any shortcut, or an installed Steam game. */
export function isLaunchable(o: any): boolean {
  return o?.app_type === SHORTCUT_APP_TYPE || !!(o?.local_per_client_data?.installed ?? o?.installed);
}

/** Installed Steam games plus all non-Steam shortcuts, most recently played first. */
export function getLibraryApps(): LibraryApp[] {
  const cs = w.collectionStore;
  const apps = new Map<number, LibraryApp>();
  const add = (list: any[] | undefined, filter?: (o: any) => boolean) => {
    for (const o of list ?? []) {
      if (apps.has(o?.appid) || (filter && !filter(o))) continue;
      const app = toLibraryApp(o);
      if (app) apps.set(app.appid, app);
    }
  };

  add(cs?.localGamesCollection?.allApps);
  add(cs?.deckDesktopApps?.allApps);
  if (apps.size === 0) {
    // Fallback for client builds without the collections above.
    add(
      cs?.allAppsCollection?.allApps,
      (o) => o?.app_type === SHORTCUT_APP_TYPE || o?.local_per_client_data?.installed || o?.installed,
    );
  }
  return [...apps.values()].sort((a, b) => b.lastPlayed - a.lastPlayed || a.name.localeCompare(b.name));
}

/** Fetch a shortcut's exe / start dir / launch options from Steam. */
export function getShortcutDetails(appid: number, timeoutMs = 3000): Promise<ShortcutDetails | null> {
  const toDetails = (d: any): ShortcutDetails | null =>
    d?.strShortcutExe
      ? { exe: d.strShortcutExe, startDir: d.strShortcutStartDir ?? "", launchOptions: d.strShortcutLaunchOptions ?? "" }
      : null;

  const cached = toDetails(w.appDetailsStore?.GetAppDetails?.(appid));
  if (cached) return Promise.resolve(cached);

  return new Promise((resolve) => {
    let done = false;
    let registration: { unregister?: () => void } | undefined;
    const finish = (d: ShortcutDetails | null) => {
      if (done) return;
      done = true;
      registration?.unregister?.();
      resolve(d);
    };
    try {
      registration = w.SteamClient.Apps.RegisterForAppDetails(appid, (d: any) => {
        const details = toDetails(d);
        if (details) finish(details);
      });
      // The callback can fire synchronously, before registration was assigned.
      if (done) registration?.unregister?.();
    } catch {
      finish(null);
    }
    setTimeout(() => finish(null), timeoutMs);
  });
}

/** Steam's own "Switch to Desktop", if this client exposes it. */
export function steamSwitchToDesktop(): boolean {
  const fn = w.SteamClient?.System?.SwitchToDesktop;
  if (typeof fn !== "function") return false;
  fn();
  return true;
}
