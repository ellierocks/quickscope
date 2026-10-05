# Changelog

Releases before 1.0.0 were pre-releases, tested on a Steam Deck LCD.

## 1.3.0

- **Faster stream starts:** when you pick a stream entry, Quickscope asks the host to start the app right away, while the Deck is still leaving Gaming Mode, and Moonlight then picks up the running app instead of starting it. Apps that take a moment to start on the host (Steam Big Picture took 1.8 s in testing) no longer add that time to the wait. It uses Moonlight's own pairing, never saved anywhere else. An app the host is already running is left alone, and a launch you cancel quits the app it started.
- **The loading screen stays up until the picture arrives,** above Moonlight's windows, and follows Moonlight's own steps: one headline for the whole launch and the current step beneath it ("Starting RTSP handshake…", "Waiting for video…"). Before, Moonlight's own loading screen showed through at the end.
- **Moonlight's questions aren't hidden:** when the host is running another app, Moonlight asks whether to quit it, and the loading screen now steps aside for that question instead of covering it.
- **"Returning to Gaming Mode…" appears the moment you quit a stream,** so Moonlight's windows closing no longer flash on screen.

## 1.2.0

- Stream entries stay up to date with the host: each time the panel opens, Quickscope asks your hosts for their current apps (through Moonlight itself, without opening it), so apps removed on the host disappear and new ones show up. Until now the list was whatever Moonlight's own window last saw. Apps hidden in Moonlight stay hidden, and a host that can't be reached keeps its last list.

## 1.1.0

- Moonlight as an AppImage: with an AppImage added to Steam as the Moonlight shortcut, stream entries start that AppImage, and its settings are used for the Moonlight settings override.

## 1.0.2

- PyroWave and VRR no longer show up in the Moonlight settings with upstream Moonlight after switching back from Nonary's fork. Quickscope now checks the installed Moonlight itself instead of a setting the fork leaves behind.

## 1.0.1

- Shorter, clearer descriptions in the panel and on the Moonlight settings page, and a more concise README.

## 1.0.0

- First stable release.
- **Moonlight gets the thread priority it asks for:** its frame pacing and audio threads run at raised priority, as Moonlight intends. Inside Flatpak its own request fails on SteamOS (the desktop portal can't map the sandbox's thread IDs), in Gaming Mode too, so Quickscope asks the system's RealtimeKit for it.
- **Moonlight settings override:** automatic bitrate (Moonlight's own default for the resolution, frame rate and YUV 4:4:4, or Nonary's fork's for PyroWave, including HDR), plus YUV 4:4:4, the performance stats overlay and Keep the screen awake. The bitrate slider goes up to 3 Gbps for PyroWave.
- **Reconnect dropped streams** (on by default): when a stream entry's connection drops, after the Deck sleeps or while the host restarts its app, Quickscope covers Moonlight's error with a loading screen and starts the stream again once the network is back, for up to 3 minutes. A host ending the stream on purpose still ends the session.
- **Idle settings:** how long before the Deck sleeps and the screen turns off without input, for the session only. They default to SteamOS's own (on battery: 5 minutes and 1 minute).
- **Battery at a tap of …:** the level and the time left, or the time to full while charging, from the average current over the last 5 minutes.
- **Low-battery warnings** at 20% and 10%, and a critical warning at 2%. Nothing warned in the session before: KDE's warning is a notification, and there's no Plasma shell to show it.
- Lighter sessions: the volume and brightness indicator no longer wakes up 10 times a second while hidden, and Wi-Fi power saving is re-checked after a reconnect instead of every 2 seconds.
- **Install from URL:** Decky's **Install Plugin from URL** with `https://github.com/ellierocks/quickscope/releases/latest/download/Quickscope.zip` installs the newest release, and the same steps update it.

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
