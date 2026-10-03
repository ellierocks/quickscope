# Quickscope

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that launches any Steam game or non-Steam shortcut **outside Gamescope**, then drops you back into Gaming Mode when it exits.

Gamescope adds a compositing step and its own frame pacing. That's fine for most games. For latency-sensitive apps, Moonlight above all, skipping it can shave noticeable input-to-photon time.

## How it works

Gamescope can't be bypassed from inside Gaming Mode, so Quickscope takes the shortest route out:

1. You pick an app in the Quick Access panel.
2. The backend stages the launch (`~/.local/state/quickscope/pending.json`). Then `quickscope_launcher.py --prepare` applies the one-shot tweaks below.
3. The backend switches to your default desktop session with `steamosctl switch-to-desktop-mode`.
4. A systemd user unit (`quickscope-launch.service`, pulled in by `plasma-core.target` and ordered after KWin) starts the launcher as soon as KWin is up. That's before the panel, desktop or autostart apps. A KDE autostart entry is kept as a fallback, and whichever runs first claims the launch.
5. The launcher waits for KWin on D-Bus, shows the loading screen, loads a temporary KWin script (fullscreen the app's first window and close the loading screen), then runs the app:
   - **Non-Steam shortcuts, Direct + Steam (default):** the shortcut's exe and launch options run directly, with no Steam overlay. `%command%` launch options are honoured. Desktop Steam starts alongside in the background and takes over the controller with your **desktop layout** once it's up, about 2 s after the app appears.
   - **Non-Steam shortcuts, Direct (fallback):** the same, but without Steam at all. The app reads the raw Deck controller, so no Steam Input layouts.
   - **Steam games:** opens `steam://rungameid/<id>` and tracks the game through Steam's `reaper SteamLaunch AppId=<id>` process. Only Steam can start Steam games, so these wait for desktop Steam behind the loading screen.
6. When the app exits, every tweak is undone and it returns to Gaming Mode with `steamos-session-select gamescope`.

### Session profile

Quickscope owns the launch session, so it's tuned for low-latency streaming by default:

- **Loading screen** from the moment KWin is up until the app's first window appears. A KWin script closes it.
- **Tearing:** KWin allows tearing for fullscreen windows by default. For Moonlight, Quickscope turns off its V-Sync and frame pacing for the session, so frames are shown immediately instead of waiting for the next vblank. Your Moonlight values are restored when it exits, so Gaming Mode is unaffected.
- **Performance mode:** GPU performance level `high` and CPU governor `performance` (via `steamosctl`), restored afterwards.
- **Quiet session:** KDE's background helpers (Baloo indexing, Discover notifier, KDE Connect, print applet) are skipped. Your own autostart apps and services, such as Syncthing, are never touched.
- **Minimal desktop:** no Plasma panel or desktop, just KWin and the app.

### One-shot tweaks

All of these live in `~/.config` or `/run`, so SteamOS updates never touch them. Each one is recorded in `undo.json` and reverted after the launch, when the plugin loads, or from **Cancel pending launch**.

| Tweak | How |
|---|---|
| Skip desktop Steam's own autostart (Quickscope starts Steam itself, or not at all) | `Hidden=true` override of `/etc/xdg/autostart/steam.desktop` in `~/.config/autostart` |
| Skip KDE background helpers | `Hidden=true` overrides of their autostart entries, plus `systemctl --user mask --runtime kde-baloo.service` |
| Skip splash | `systemctl --user mask --runtime plasma-ksplash.service` |
| Minimal desktop | `systemctl --user mask --runtime plasma-plasmashell.service` |
| Moonlight V-Sync / frame pacing off | `vsync=false`, `framepacing=false` in `Moonlight.conf`; your values are written back afterwards |
| Performance mode | `steamosctl set-gpu-performance-level high` (re-applied every 2 s, since desktop Steam resets it) and `set-cpu-scaling-governor performance` |

Quickscope starts the Steam client directly with fast-start flags (`-silent -noverifyfiles -skipinitialbootstrap -norepairfiles`). On SteamOS it bypasses the `/usr/bin/steam` wrapper, which always adds `-pipewire`. On a Wayland desktop that flag makes Steam request screen capture through a portal dialog every session.

When switching, the backend watches `gamescope-session.service`. Gamescope sometimes ignores SIGTERM and sits out its 10 s stop timeout, so if it's still stopping after 3 s, Quickscope sends the SIGKILL systemd would have sent anyway.

The systemd unit itself stays installed, but it does nothing unless a launch is staged (`ConditionPathExists`). Uninstalling the plugin removes it.

A staged launch older than 5 minutes is ignored, so entering Desktop Mode by hand later never triggers a surprise launch.

## Usage

- **A** on an app: launch outside Gamescope
- **X** on an app: pin or unpin it at the top of the panel
- **Y** on a non-Steam app: switch between **Direct + Steam** (default) and **Direct** (fallback, no Steam). With Direct + Steam, the controller uses Steam's **desktop layout**, so set that layout up the way you want it inside your apps.
- Search filters installed Steam games and every non-Steam shortcut.

### Settings

| Setting | Default | Notes |
|---|---|---|
| Return to Gaming Mode on exit | on | |
| Force fullscreen | on | Fullscreens the first normal window that isn't Steam, Plasma or a system dialog. |
| Suspend desktop compositor | on | X11 only. Uses KWin's *Suspend Compositing* shortcut (Plasma 6 has no D-Bus call for it). |
| Loading screen | on | Shown until the app's first window appears |
| Skip splash screen | on | |
| Minimal desktop | on | No panel or desktop until the app exits |
| Desktop session | System default | Follows `steamosctl get-default-desktop-session`, or forces X11 / Wayland |
| Extra launch delay | 0 s | Added after KWin is ready |

### Moonlight tips

- Add Moonlight (Flatpak) as a non-Steam shortcut and leave it on **Direct + Steam**.
- To jump straight into a stream, set the shortcut's launch options to `run com.moonlight_stream.Moonlight stream <host> "<app>" --display-mode fullscreen`. Moonlight then closes itself when the stream ends, and Quickscope returns you to Gaming Mode.

## Requirements

- SteamOS 3.x with a systemd-managed Plasma session. Tested against SteamOS 3.9 / Plasma 6.
- `steamos-session-select`, `systemctl`, `dbus-send`, `/usr/bin/python3`

## Building

```sh
pnpm install
pnpm build          # -> dist/index.js
```

Package it with the [Decky CLI](https://github.com/SteamDeckHomebrew/cli) (`defaults/quickscope_launcher.py` is copied into the plugin root), or zip a `Quickscope/` folder containing `plugin.json`, `package.json`, `main.py`, `dist/index.js` and `quickscope_launcher.py`. Install that zip from Decky's Developer settings.

## Troubleshooting

- Launcher log: `~/.local/state/quickscope/launcher.log`. It records timings: KWin ready, launch start, app exit.
- Unit log: `journalctl --user -u quickscope-launch.service`
- Backend log: `~/homebrew/logs/Quickscope/`
- Undo everything by hand: `python3 ~/.local/state/quickscope/quickscope_launcher.py --restore`
- Stuck in desktop? Use the desktop's *Return to Gaming Mode* icon.
