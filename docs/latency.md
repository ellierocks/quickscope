# Latency measurements

Moonlight's own end-of-stream statistics ("Global video stats"), taken from the Deck's journal for every stream in one test session.

## Setup

- **Client:** Steam Deck LCD (1280×800, 60 Hz, no VRR), SteamOS 3.9.2, Plasma 6.7, Moonlight 6.1.0 (fork with PyroWave support) as a Flatpak, Moonlight settings unchanged between runs: 1920×1200, 60 FPS, **V-Sync on**
- **Host:** [Vibeshine](https://github.com/Nonary/vibeshine) by Nonary (a fork of [Sunshine](https://github.com/LizardByte/Sunshine)), streaming *Batman: Arkham Knight* to the Deck over 5 GHz Wi-Fi
- **Environments:**
  - Gaming Mode (Gamescope)
  - Quickscope on KWin Wayland (the default)
  - Quickscope on KWin X11 (no longer offered)
- **Date:** 2026-10-03, all streams within 15 minutes

## Results

"Render time" is Moonlight's *Average rendering time (including monitor V-sync latency)*: the time from a decoded frame to Moonlight presenting it, including any wait imposed by the compositor's display sync. It's the stage that changes with the compositor. Averages are weighted by stream length.

| Codec | Environment | Streams | Total time | Render time | Decode time | FPS | Dropped (pacing) |
|---|---|---:|---:|---:|---:|---:|---:|
| HEVC | Gaming Mode (Gamescope) | 1 | 52 s | **7.03 ms** | 0.53 ms | 59.5 | 0.13% |
| HEVC | Quickscope (Wayland) | 3 | 106 s | **1.33 ms** | 0.46 ms | 58.0 | 0.01% |
| HEVC | Quickscope (X11) | 1 | 91 s | 7.26 ms | 0.51 ms | 48.9 | 0.00% |
| PyroWave | Gaming Mode (Gamescope) | 1 | 91 s | **0.93 ms** | 0.72 ms | 59.2 | 0.31% |
| PyroWave | Quickscope (Wayland) | 4 | 346 s | **0.51 ms** | 0.63 ms | 51.3 | 0.24% |

### What this shows

- **HEVC: render time fell from 7.03 ms to 1.33 ms, about 5.7 ms (81%) less**, with Quickscope on Wayland versus Gaming Mode. Decode time was the same in both, so the difference is in presentation, i.e. the compositor.
- **PyroWave: 0.93 ms to 0.51 ms, about 0.4 ms (45%) less.** PyroWave frames reach the screen quickly anyway, so there's much less for the compositor to add.
- **X11 was no better than Gamescope** (7.26 ms for HEVC), which is one reason Quickscope only uses Wayland.

### Caveats

- These are small samples from a single session: one Gaming Mode stream per codec, 52–91 s each. Treat them as a strong indication, not a benchmark.
- Moonlight's statistics stop when the frame is handed to the compositor. Anything after that, before the panel lights up, isn't included. Only an end-to-end measurement, such as a high-speed camera, captures that.
- Frame rates differ between runs because the game's own frame rate varied with the scene.

## Power profiles

Measured later the same day, PyroWave at the same Moonlight settings. "Chip power" is the APU's own reading (`power1_input` of the `amdgpu` hwmon), averaged over 10–20 s mid-stream, so it doesn't depend on whether the Deck is charging.

| Profile | Stream | Render | Decode | FPS | Dropped (pacing) | Chip power |
|---|---|---:|---:|---:|---:|---:|
| High performance | the four Quickscope PyroWave streams below | 0.51 ms | 0.63 ms | 51.3 | 0.24% | 8.5 W |
| Battery saver | 234 s, menus then about a minute of gameplay | 0.50 ms | 0.71 ms | 47.2 | 0.05% | 5.1 W |

With the same stream running, switching profiles live gave 8.5 W (High performance), 7.8 W (SteamOS defaults), 4.8 W (Battery saver) and 5.2 W (Battery saver plus a 6 W TDP limit). Nearly all of the saving comes from turning off CPU boost: with it on, the CPU idles around 3 GHz although Moonlight barely uses it. The TDP limit added nothing.

**CPU scheduler.** One stream each, same scene, Battery saver, PyroWave:

| Scheduler | Render | Decode | Frame queue | Dropped (pacing) | Host FPS | Chip power | CPU busy |
|---|---:|---:|---:|---:|---:|---:|---:|
| `scx_lavd` | 0.48 ms | 0.65 ms | 2.42 ms | 0.09% | 26 | 4.18 W | 10.0% |
| kernel default (`none`) | 0.54 ms | 0.66 ms | 2.06 ms | 0.04% | 23 | 3.14 W | 6.1% |

Latency is the same within noise; the kernel's scheduler used about 1 W less. The host sent about 12% fewer frames in the second run, which explains only part of that. Quickscope uses the kernel's scheduler for the session and restores the user's afterwards.

The battery saver's power reading was taken early in the stream, before the gameplay section. Render time didn't change and decode time rose by about 0.08 ms, which is negligible.

## Every stream

| Time | Codec | Environment | Length | Render | Decode | FPS |
|---|---|---|---:|---:|---:|---:|
| 07:28:40 | PyroWave | Quickscope (Wayland) | 86 s | 0.43 ms | 0.68 ms | 49.9 |
| 07:31:02 | PyroWave | Quickscope (Wayland) | 135 s | 0.52 ms | 0.65 ms | 45.2 |
| 07:33:17 | PyroWave | Quickscope (Wayland) | 106 s | 0.52 ms | 0.58 ms | 59.0 |
| 07:33:38 | PyroWave | Quickscope (Wayland) | 19 s | 0.67 ms | 0.57 ms | 59.1 |
| 07:35:01 | HEVC | Quickscope (Wayland) | 72 s | 1.11 ms | 0.46 ms | 59.6 |
| 07:35:23 | HEVC | Quickscope (Wayland) | 19 s | 0.92 ms | 0.45 ms | 58.8 |
| 07:35:44 | HEVC | Quickscope (Wayland) | 15 s | 2.88 ms | 0.45 ms | 48.9 |
| 07:38:53 | HEVC | Quickscope (X11) | 91 s | 7.26 ms | 0.51 ms | 48.9 |
| 07:40:39 | HEVC | Gaming Mode (Gamescope) | 52 s | 7.03 ms | 0.53 ms | 59.5 |
| 07:42:20 | PyroWave | Gaming Mode (Gamescope) | 91 s | 0.93 ms | 0.72 ms | 59.2 |

## Method

Each stream's statistics, codec and environment come from the user journal (`journalctl --user`):

- **Codec:** `PyroWave decoding …` lines, or FFmpeg's `[hevc @ …]` lines, before each stream.
- **Environment:** whichever compositor last started before the stream ("Gamescope Session", "KDE Wayland Compositor" or "KDE Window Manager").
- **Length:** from Moonlight's `/launch` or `/resume` request to its stats block.
- **V-Sync:** Moonlight logged V-Sync enabled for every stream.
