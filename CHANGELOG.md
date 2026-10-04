# Changelog

All releases so far are pre-releases, tested on a Steam Deck LCD.

## 0.9.0

- **Ways out of a stuck session:** hold **…** for 3 seconds to close the app and return to Gaming Mode. If the launcher itself crashes, a recovery job undoes the session and returns.
- **Faster:** preparation and the return's clean-up are about a second shorter each (one `systemctl` call instead of three). Gamescope's own process is killed 0.3 s into its shutdown, so it can no longer hang or abort with a crash dump on the way out.
- **Loading screens:** Quickscope draws its own spinner (the same on every Deck), the launching page fades out before the switch, and the pointer is hidden. The returning screen has no spinner, since it may stay frozen on screen through the switch back.
- **First run:** the panel explains what's missing when Moonlight isn't installed or has no paired hosts yet.
- Steam game launches are labelled as slower.

## 0.8.0

- **Direct-to-stream entries:** Moonlight's paired hosts and their apps are listed in the panel. A stream entry runs Moonlight's `stream` command, and the loading screen stays up until the video has started.
- Pin stream entries to the top of the panel.
- The panel leads with streams and Moonlight; other apps are found by search.
- **Save diagnostics:** a report for bug reports, with network names and addresses masked.
- The launcher log and a pending Wi-Fi unlock survive plugin updates.

## 0.7.0

- **Network settings:** Wi-Fi power saving off for the session, and the Wi-Fi connection locked to its access point (WPA Supplicant backend). Each is only shown where it applies.
- With the WPA Supplicant backend, the loading screen waits for the network after SteamOS restarts NetworkManager.
- Sessions use the kernel's own CPU scheduler instead of `scx_lavd`: the same latency on about 1 W less.

## 0.6.0

- **Per-display Moonlight settings** (opt-in): resolution, frame rate, codec, bitrate, V-Sync, frame pacing and HDR, with PyroWave and VRR for Nonary's fork.
- The brightness shortcut moves to **…** + left stick; desktop Steam's window is kept minimized.
- Lower background CPU use; linting and formatting in CI.

## 0.5.1

- Display scale slider, defaulting to 100%.

## 0.5.0

- **Display options:** match Gaming Mode's resolution, refresh rate and HDR, or pick one of the display's own modes, on external displays and multi-mode panels.

## 0.4.0

- **Power profiles:** Automatic (follows the charger), High performance and Battery saver.
- The loading screen, splash skip, minimal desktop, brightness shortcut and volume indicator are always on.

## 0.3.0

- Brightness shortcut.

## 0.2.0

- Brightness lock and a volume indicator.
- Launching page shown immediately; shorter black screens on launch and return.

## 0.1.0

- First release: launch games and apps outside Gamescope from Gaming Mode, with Hybrid and Direct launch methods and a Quickscope button on game pages.
