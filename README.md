<div align="center">

# Quickscope

**Launch Moonlight and other streaming clients outside Gamescope, straight from Gaming Mode.**

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for the Steam Deck

</div>

Quickscope hands the screen to a single app in a stripped-down Plasma Wayland session (KWin, no panel, nothing in the background) and returns you to Gaming Mode when the app exits. From pressing **A** to Moonlight on screen takes about **6 seconds**.

## Why

Moonlight's own statistics, streaming *Batman: Arkham Knight* from [Vibeshine](https://github.com/Nonary/vibeshine) (Nonary's fork of Sunshine) to a Steam Deck LCD, with identical Moonlight settings (V-Sync on):

| Codec | Gaming Mode (Gamescope) | Quickscope | Difference |
|---|---:|---:|---:|
| HEVC | 7.03 ms | **1.33 ms** | −5.7 ms (−81%) |
| PyroWave | 0.93 ms | **0.51 ms** | −0.4 ms (−45%) |

*Average rendering time, including display sync: the stage the compositor affects. Decode time was the same in both.* Small samples from one session; full data, method and caveats in [docs/latency.md](docs/latency.md).

## Features

- **One button press** from the Quick Access menu (pinned and recent apps, search) or from a game's library page
- **Fast startup:** the app launches the moment the compositor is up, with a loading screen instead of a black screen
- **Your controller layout** keeps working through Steam's desktop layout
- **A quiet session:** no Plasma panel, splash screen or KDE background helpers, with a high performance or battery saver profile
- **Brightness and volume controls** in place of the Quick Access menu
- **Leaves no trace:** every change is session-only and undone on exit. Your own apps, services and app settings are never touched.

## Install

> Requires a Steam Deck on **SteamOS 3.9+** with [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader).

1. Download **`Quickscope.zip`** from the [latest release](../../releases/latest).
2. In Decky, go to **Settings → Developer → Install Plugin from ZIP**. Enable Developer mode first if you don't see it.
3. Pick the downloaded file.

## Usage

Open **Quickscope** from the Quick Access menu (**⋯**), or press the **Quickscope** button on a game's library page (bottom-right of the header art, above the Play row). Play itself still launches in Gaming Mode as usual.

| Button | |
|:---:|---|
| **A** | Launch outside Gamescope |
| **X** | Pin / unpin |
| **Y** | Switch launch method (non-Steam apps) |

| Launch method | What happens | Controller |
|---|---|---|
| **Hybrid** *(default)* | The app starts right away, and Steam starts in the background | Steam's **desktop layout**, from about 2 s in |
| **Direct** | The app starts right away, without Steam. Good for apps that don't need trackpad mouse or Steam Input | Raw Deck controller, no Steam Input |
| *Steam games* | Launched through desktop Steam, which takes about 13 s to start | The game's usual layout |

> [!NOTE]
> Quickscope is built for **streaming clients** like Moonlight, where Gamescope's added latency is measurable (see below). Steam games work, but desktop Steam has to start first and local games have little to gain, so Gaming Mode is usually the better choice for them.

> [!TIP]
> With **Hybrid**, set up your desktop layout (*Steam → Settings → Controller → Desktop Layout*) the way you want it inside your apps. For Moonlight, a gamepad layout with trackpad mouse and paddle clicks works well.

### Moonlight

- Add Moonlight (Flatpak) as a non-Steam shortcut and leave it on **Hybrid**. Your Moonlight settings are used as-is unless you turn on the override below.
- To jump straight into a stream, use the launch options `run com.moonlight_stream.Moonlight stream <host> "<app>"`. Moonlight then closes when the stream ends, which returns you to Gaming Mode.
- Quit a stream with **L1 + R1 + Start + Select**.

**Per-display Moonlight settings (optional).** *Settings → Moonlight settings* opens a page with an **Override Moonlight settings** toggle, off by default. Turned on, each display gets its own resolution (including above the screen's, for supersampling), frame rate, codec, bitrate, V-Sync, frame pacing and HDR, picked automatically by the display Moonlight runs on: one set for the Deck's screen, another for your TV. With [Nonary's VRR fork](https://github.com/Nonary/moonlight-qt) it also offers PyroWave and, on VRR displays, VRR with the fork's VRR frame rates; neither appears with upstream Moonlight. While the override is on, Moonlight follows these settings: Quickscope swaps them into Moonlight's settings for the session and puts your own values back afterwards, so change them on this page rather than in Moonlight. Moonlight settings the page doesn't cover are never touched.

### Settings

| Setting | Default |
|---|---|
| Return to Gaming Mode on exit | On |
| Force fullscreen | On |
| Power profile: Automatic (Battery saver on battery, High performance when plugged in), High performance, or Battery saver (about 40% less chip power while streaming) | Automatic |
| Starting brightness | Matches Gaming Mode; or a fixed level |
| Lock brightness (stops KDE's power management changing it) | On |
| Display mode and HDR (on an external display, or a built-in panel with several modes or HDR): match Gaming Mode, or pick one of the display's own modes | Match Gaming Mode |
| Scale (size of the app's menus and text) | 100% |

There's no Quick Access menu outside Gaming Mode, so Quickscope adds its own controls:

- **Brightness:** hold **…** and push the **left stick** up or down.
- **Volume:** the volume buttons work as usual, with an on-screen indicator.

## Troubleshooting

- **Stuck on the desktop?** Use *Return to Gaming Mode*, or run `steamos-session-select gamescope`.
- **No loading screen when docked?** Expected: a TV takes a few seconds to re-sync after the session switch, and the app is usually open before the picture comes back.
- **Decky disappeared after several very short sessions in a row?** Decky's crash protection disables it when Steam's UI goes away three times within about a minute, and each Quickscope round trip closes Steam's UI twice. Restart the Deck (or `sudo systemctl start plugin_loader`) to bring it back. Normal sessions longer than a minute don't trigger it.
- **Logs:** `~/.local/state/quickscope/launcher.log` (launch timings and every change made or undone), and Decky's log in `~/homebrew/logs/Quickscope/`.
- **Undo everything by hand:** `python3 ~/.local/state/quickscope/quickscope_launcher.py --restore`

<details>
<summary><b>How it works</b></summary>

1. The plugin stages the launch, applies the session tweaks below, and switches to Plasma on Wayland (`steamosctl switch-to-desktop-mode plasma.desktop`). Wayland is always used: on X11, apps could leave fullscreen and startup was slower. If Gamescope hangs while shutting down, Quickscope ends it after 3 s instead of waiting out systemd's 10 s timeout.
2. A systemd user unit, `quickscope-launch.service`, starts the launcher as soon as KWin is up, before Plasma's panel or autostart apps.
3. The launcher shows the loading screen, loads a temporary KWin script (fullscreen and focus the app's first window, then close the loading screen) and starts the app.
4. When the app exits, every tweak is undone and the Deck returns to Gaming Mode.

**Session tweaks.** Each one is recorded in `~/.local/state/quickscope/undo.json` and undone on exit. If that's interrupted, they're undone when the plugin next loads, or from **Cancel pending launch**.

| Tweak | How |
|---|---|
| Steam's own desktop autostart skipped | `Hidden=true` override in `~/.config/autostart` |
| KDE helpers skipped (Baloo, Discover notifier, KDE Connect, print applet) | `Hidden=true` overrides, `systemctl --user mask --runtime kde-baloo.service` |
| No splash screen | `Engine=none` in `~/.config/ksplashrc` (KDE's own switch), restored afterwards |
| No Plasma panel | `systemctl --user mask --runtime plasma-plasmashell.service` |
| Brightness | Writes the backlight (`/sys/class/backlight/*/brightness`) and re-applies it if KDE changes it (for the whole session with the lock, otherwise while the desktop starts); the original level is restored afterwards |
| Display | Gaming Mode's mode is read with `modetest` before switching, then set with `kscreen-doctor` (external displays: the Deck's screen off, as in Gaming Mode). Only what differs is changed, since every change makes a TV re-sync. KDE's new-display dialog (`plasma-kscreen-osd.service`) is masked for the session, and `~/.config/kwinoutputconfig.json` is restored afterwards |
| Brightness shortcut | Reads the built-in controller's hidraw reports without grabbing them, so Steam and the app still get every input |
| Power profile *(setting)* | High performance: GPU level `high`, `performance` governor. Battery saver: GPU `auto`, `powersave` governor, CPU boost off. Both use the kernel's own CPU scheduler instead of `scx_lavd` (same latency, about 1 W less while streaming). Automatic picks one from `/sys/class/power_supply` and switches if you plug in or unplug. Set with `steamosctl`, re-applied while running, restored afterwards |

Everything lives in `~/.config` or `/run`, never in files a SteamOS update replaces. If Quickscope had to create `ksplashrc`, it deletes it again afterwards.

Steam is started as `/usr/lib/steam/steam -steamdeck -silent -noverifyfiles -skipinitialbootstrap -norepairfiles`. It bypasses the `/usr/bin/steam` wrapper, whose `-pipewire` flag triggers a screen-capture permission prompt every session on Wayland. The flags also skip the file check Steam runs after Gaming Mode's Steam is shut down abruptly.

`quickscope-launch.service` stays installed but does nothing unless a launch is staged. A staged launch older than 5 minutes is ignored. Uninstalling the plugin removes the unit and `~/.local/state/quickscope`.

</details>

<details>
<summary><b>Development</b></summary>

```sh
pnpm install
pnpm typecheck
pnpm lint       # ESLint, Prettier and Ruff (Ruff runs through uvx)
pnpm format     # fix what lint can fix
pnpm test       # Python unit tests (backend + launcher)
pnpm package    # build out/Quickscope.zip
```

Pushing a tag that matches `package.json`'s version (e.g. `v0.1.0`) builds the zip and publishes a GitHub release.

Tested on a Steam Deck LCD with SteamOS 3.9.2, Plasma 6.7 and Decky Loader 3.2.

</details>

## License

[BSD-3-Clause](LICENSE)
