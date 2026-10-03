# Quickscope

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that launches a non-Steam app or Steam game **outside Gamescope**, in a stripped-down desktop session tuned for low latency, then drops you back into Gaming Mode when it exits.

It's built for game streaming with **Moonlight**. Inside Gaming Mode, every frame goes through Gamescope's compositor. Quickscope gives the app the display to itself instead, with the GPU kept at high clocks and nothing else competing.

From pressing **A** in Gaming Mode to Moonlight on screen takes about **6 seconds** on a Steam Deck.

## Features

- **One press from Gaming Mode.** Pick an app in the Quick Access panel. Pinned apps sit at the top, and recently played apps and search are below.
- **Fast:** the app starts as soon as KWin is up, before Plasma's panel, desktop or autostart apps.
- **Keeps your controller layout:** desktop Steam starts alongside the app and takes over the controller with your Steam **desktop layout** (see [Controls](#controls)).
- **Tuned session:** performance mode, no panel, no splash, and no KDE background helpers. Your apps' own settings are never changed.
- **Loading screen** until the app's window appears, never a black screen.
- **Leaves no trace:** every change is session-only, lives in `~/.config` or `/run` (never in files a SteamOS update replaces), and is undone when the app exits.

## Requirements

- Steam Deck running **SteamOS 3.9 or later**, with the default Plasma 6 desktop
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader)

Tested on a Steam Deck LCD with SteamOS 3.9.2, Plasma 6.7 and Decky Loader 3.2. Other SteamOS-like distributions may work if they have `steamosctl` (or `steamos-session-select`) and a systemd-managed Plasma 6 session, but they're untested.

## Install

1. Download `Quickscope.zip` from the [latest release](../../releases/latest).
2. In Decky, open **Settings → Developer** (enable Developer mode under General if it's hidden).
3. Choose **Install Plugin from ZIP** and pick the downloaded file.

## Usage

Open Quickscope from the Quick Access menu (**⋯**):

| Button | Action |
|---|---|
| **A** | Launch the app outside Gamescope |
| **X** | Pin or unpin the app |
| **Y** | Non-Steam apps: switch between **Hybrid** (default) and **Direct** |

Quickscope warns you if a game is running, because leaving Gaming Mode closes it. When the app exits, you're returned to Gaming Mode.

### Controls

- **Hybrid** (default for non-Steam apps): the app starts immediately, and desktop Steam starts in the background. About 2 s after the app's window appears, Steam takes over the controller with your **desktop layout**. Set that layout up for how you use your apps. For Moonlight, a gamepad layout with trackpad mouse and paddle clicks works well. *(Steam → Settings → Controller → Desktop Layout)*
- **Direct**: no Steam at all. The app reads the Deck's controller directly, so there are no Steam Input layouts.
- **Steam games** can only be started by Steam, so they wait for desktop Steam to start behind the loading screen, then use their usual layout.

### Moonlight

- Add Moonlight (Flatpak) as a non-Steam shortcut and leave it on **Hybrid**.
- Moonlight runs with your own settings. Quickscope never touches its config. On the Deck's 60 Hz screen, which has no VRR, keeping V-Sync on avoids tearing.
- To jump straight into a stream, set the shortcut's launch options to `run com.moonlight_stream.Moonlight stream <host> "<app>"`. Moonlight closes itself when the stream ends, and Quickscope returns you to Gaming Mode.
- Quit a stream with **L1 + R1 + Start + Select**.

### Settings

| Setting | Default | |
|---|---|---|
| Return to Gaming Mode on exit | on | |
| Force fullscreen | on | Fullscreens the app's first window |
| Suspend desktop compositor | on | Only matters if you force an X11 session |
| Loading screen | on | Shown until the app's window appears |
| Skip splash screen | on | |
| Minimal desktop | on | No Plasma panel or desktop, just the app |
| Desktop session | System default | Or force Plasma X11 / Wayland |
| Extra launch delay | 0 s | |

## What a launch does

1. The plugin stages the launch and applies the session tweaks below, then switches to your default desktop session with `steamosctl switch-to-desktop-mode`.
2. Gamescope sometimes ignores the request to quit and would hold things up for 10 s. If it's still stopping after 3 s, Quickscope ends it the way systemd would at 10 s.
3. A systemd user unit, `quickscope-launch.service`, runs the launcher as soon as KWin is up. The launcher shows the loading screen, loads a temporary KWin script (fullscreen and focus the app's first window, close the loading screen), and starts the app.
4. When the app exits, everything is undone and the Deck returns to Gaming Mode.

### Session tweaks

All of these apply only for the launch session and are recorded in `~/.local/state/quickscope/undo.json`. They're undone when the app exits. If that's interrupted, they're also undone when the plugin next loads or from **Cancel pending launch**.

| Tweak | How |
|---|---|
| Steam's own desktop autostart skipped (Quickscope starts Steam itself, or not at all) | `Hidden=true` override in `~/.config/autostart` |
| KDE background helpers skipped (Baloo indexing, Discover notifier, KDE Connect, print applet) | `Hidden=true` overrides, `systemctl --user mask --runtime kde-baloo.service` |
| No splash screen | `systemctl --user mask --runtime plasma-ksplash.service` |
| Minimal desktop | `systemctl --user mask --runtime plasma-plasmashell.service` |
| Performance mode | `steamosctl set-gpu-performance-level high` (re-applied while the app runs, because desktop Steam resets it) and `set-cpu-scaling-governor performance` |

Your own autostart apps and services (such as Syncthing) and your apps' own settings are never touched.

Steam is started directly as `/usr/lib/steam/steam -steamdeck -silent -noverifyfiles -skipinitialbootstrap -norepairfiles`. It skips the `/usr/bin/steam` wrapper because the wrapper adds `-pipewire`, which on a Wayland desktop makes Steam ask for screen-capture permission every session. The flags also skip the file check Steam would otherwise run after Gaming Mode's Steam is shut down abruptly.

`quickscope-launch.service` stays installed but does nothing unless a launch is staged. A staged launch older than 5 minutes is ignored, so entering Desktop Mode yourself never triggers one. Uninstalling the plugin removes the unit and `~/.local/state/quickscope`.

## Troubleshooting

- **Launcher log:** `~/.local/state/quickscope/launcher.log` (timings and every tweak applied or undone)
- **Backend log:** `~/homebrew/logs/Quickscope/`
- **Stuck in the desktop:** use *Return to Gaming Mode*, or run `steamos-session-select gamescope`.
- **Undo everything by hand:** `python3 ~/.local/state/quickscope/quickscope_launcher.py --restore`

## Development

```sh
pnpm install
pnpm typecheck
pnpm test       # Python unit tests (backend + launcher)
pnpm package    # build and write out/Quickscope.zip
```

Pushing a tag that matches `package.json`'s version (e.g. `v0.1.0`) builds the zip and publishes a GitHub release.

## License

[BSD-3-Clause](LICENSE)
