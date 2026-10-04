import asyncio
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import time

import decky

SETTINGS_DEFAULTS = {
    "return_to_gaming": True,
    "force_fullscreen": True,
    # "performance": GPU clocks high, performance governor. "battery": GPU on
    # auto, powersave governor, CPU boost off (about 40% less APU power).
    # "auto": battery on battery, performance when plugged in.
    "power_profile": "auto",
    # Session-only, restored afterwards: Wi-Fi power saving off, and the Wi-Fi
    # connection locked to its current access point (no roaming or scans).
    "wifi_powersave_off": True,
    "wifi_lock_ap": True,
    # Hold the screen at one brightness for the session (there's no Quick
    # Access menu to change it): Gaming Mode's level, or brightness_pct.
    "lock_brightness": True,
    "match_gaming_brightness": True,
    "brightness_pct": 50,
    # "" matches Gaming Mode's display mode; otherwise a mode like
    # "1920x1080@60.00", used when the display offers it.
    "display_mode": "",
    # HDR with a forced display mode, on displays that support it. Matching
    # Gaming Mode follows Gamescope's HDR state instead.
    "display_hdr": False,
    # Percent; KDE would otherwise pick its own (170% on a 4K TV).
    "display_scale": 100,
    # Opt-in: Moonlight settings per display ({display id: profile}), swapped
    # into Moonlight.conf for the session. Off unless the user turns it on.
    "moonlight_override": False,
    "moonlight_profiles": {},
    "favorites": [],
    # Pinned direct-to-stream entries: [{"host": name, "app": exact app name}].
    "pinned_streams": [],
    # Per-shortcut overrides, {"<appid>": "direct"}. Shortcuts default to
    # "hybrid" (launched directly, desktop Steam started alongside for its
    # desktop controller layout); Steam games always launch through Steam.
    "launch_modes": {},
}

# Always Plasma on Wayland: on X11 apps could leave fullscreen and startup was slower.
WAYLAND_SESSION = "plasma.desktop"
LAUNCH_MODES = ("hybrid", "direct")
POWER_PROFILES = ("auto", "performance", "battery")
# Moonlight's videocfg: auto, H.264, HEVC, AV1, PyroWave (Nonary's fork). 3 is
# a deprecated HEVC HDR value.
MOONLIGHT_CODECS = (0, 1, 2, 4, 5)
DEFAULT_SHORTCUT_MODE = "hybrid"

# Must match PENDING_MAX_AGE in quickscope_launcher.py.
PENDING_MAX_AGE = 300

SHORTCUT_GAMEID_FLAG = 0x02000000

LAUNCHER_TIMEOUT = 30

# Must match UNIT_NAME in quickscope_launcher.py.
LAUNCH_UNIT = "quickscope-launch.service"

GAMESCOPE_UNIT = "gamescope-session.service"
# Normal Gamescope stops take ~1-2.2 s on the Deck; its unit's own timeout is 10 s.
GAMESCOPE_STOP_GRACE = 3
# Gamescope's own process is killed this long after its stop begins.
GAMESCOPE_MAIN_GRACE = 0.3
# Gamescope's process name (its crash dumps are "core.gamescope-wl…").
GAMESCOPE_PROCESS_NAMES = ("gamescope-wl", "gamescope")
SIGKILL = getattr(signal, "SIGKILL", 9)  # Linux's; absent where the tests may run
GAMESCOPE_WATCH_TIMEOUT = 15


def _user():
    try:
        pw = pwd.getpwnam(decky.DECKY_USER)
        return pw.pw_uid, pw.pw_dir
    except KeyError:
        return os.getuid(), decky.DECKY_USER_HOME


def _paths():
    _, home = _user()
    state = os.path.join(home, ".local", "state", "quickscope")
    return {
        "state": state,
        "pending": os.path.join(state, "pending.json"),
        "launcher": os.path.join(state, "quickscope_launcher.py"),
    }


def _system_env():
    """Environment suitable for running system binaries from the Decky backend."""
    env = dict(os.environ)
    # Decky's PyInstaller bundle overrides LD_LIBRARY_PATH, which breaks
    # system binaries.
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    uid, home = _user()
    runtime = f"/run/user/{uid}"
    env["HOME"] = home
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    env["PATH"] = ":".join(p for p in (env.get("PATH", ""), "/usr/local/bin:/usr/bin:/bin") if p)
    return env


def _which(cmd):
    return shutil.which(cmd, path=_system_env()["PATH"])


async def _steamosctl_value(command):
    """The value from a `steamosctl get-…` command ("Label: value"), or None.
    E.g. get-wifi-backend: "iwd" (SteamOS's default) or "wpa_supplicant";
    get-wifi-power-management-state: "enabled" or "disabled"."""
    steamosctl = _which("steamosctl")
    if not steamosctl:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            steamosctl,
            command,
            env=_system_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace").rsplit(":", 1)[-1].strip() or None


MOONLIGHT_FLATPAK = "com.moonlight_stream.Moonlight"


def _moonlight_stream_command(host, app):
    """Moonlight's `stream <host> <app>` command: the Flatpak (user or system
    install), else a native `moonlight`. None if neither is installed."""
    flatpak_dirs = [
        os.path.join(decky.DECKY_USER_HOME, ".local", "share", "flatpak", "app", MOONLIGHT_FLATPAK),
        os.path.join("/var/lib/flatpak/app", MOONLIGHT_FLATPAK),
    ]
    if any(os.path.isdir(d) for d in flatpak_dirs) and _which("flatpak"):
        base = ["flatpak", "run", MOONLIGHT_FLATPAK]
    elif _which("moonlight"):
        base = ["moonlight"]
    else:
        return None
    return shlex.join([*base, "stream", host, app])


def _launcher_source():
    # The decky CLI copies defaults/* into the plugin root; during development
    # the file is still under defaults/.
    for candidate in (
        os.path.join(decky.DECKY_PLUGIN_DIR, "quickscope_launcher.py"),
        os.path.join(decky.DECKY_PLUGIN_DIR, "defaults", "quickscope_launcher.py"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _install_launcher():
    """Copy the launcher out of the plugin dir so a plugin update or removal
    mid-session can't pull it out from under the desktop."""
    source = _launcher_source()
    if source is None:
        return None
    dest = _paths()["launcher"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source, dest)
    return dest


def _clean_moonlight_profile(profile):
    clean = {
        "name": str(profile.get("name", ""))[:60],
        "width": max(320, min(7680, int(profile["width"]))),
        "height": max(200, min(4320, int(profile["height"]))),
        "fps": max(10, min(240, int(profile["fps"]))),
        "vsync": bool(profile["vsync"]),
        "framepacing": bool(profile["framepacing"]),
    }
    # Added later; profiles without them leave Moonlight's own value alone.
    if profile.get("bitrate") is not None:
        clean["bitrate"] = max(500, min(1000000, int(profile["bitrate"])))  # kbps
    if profile.get("hdr") is not None:
        clean["hdr"] = bool(profile["hdr"])
    if profile.get("codec") is not None and int(profile["codec"]) in MOONLIGHT_CODECS:
        clean["codec"] = int(profile["codec"])
    if profile.get("vrr_capable") is not None:
        clean["vrr_capable"] = bool(profile["vrr_capable"])
    if profile.get("vrr") is not None:
        clean["vrr"] = bool(profile["vrr"])
        if clean["vrr"]:
            # The VRR fork only uses VRR with V-Sync on.
            clean["vsync"] = True
    return clean


async def _run_launcher(*args):
    launcher = _paths()["launcher"]
    if not os.path.isfile(launcher):
        return 1, "launcher not installed"
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else (_which("python3") or "python3")
    proc = await asyncio.create_subprocess_exec(
        python,
        launcher,
        *args,
        env=_system_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=LAUNCHER_TIMEOUT)
    except TimeoutError:
        proc.kill()
        return 1, f"launcher {' '.join(args)} timed out"
    return proc.returncode, out.decode(errors="replace").strip()


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _pending_is_fresh():
    try:
        with open(_paths()["pending"]) as f:
            created = json.load(f).get("created", 0)
    except (OSError, ValueError):
        return False
    return time.time() - created <= PENDING_MAX_AGE


async def _systemctl_user(*args):
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        *args,
        env=_system_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def _unit_processes(unit):
    """[(pid, process name)] in a user unit's cgroup."""
    _, cgroup = await _systemctl_user("show", "-p", "ControlGroup", "--value", unit)
    procs = []
    try:
        with open(os.path.join("/sys/fs/cgroup", cgroup.strip().lstrip("/"), "cgroup.procs")) as f:
            pids = [int(p) for p in f.read().split()]
    except (OSError, ValueError):
        return procs
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as f:
                procs.append((pid, f.read().strip()))
        except OSError:
            pass
    return procs


async def _hurry_gamescope_stop():
    """Get Gamescope out of the way fast when leaving Gaming Mode.

    Left to stop by itself, Gamescope takes 1.1-1.4 s, often aborts and has a
    crash dump written (~2 s), or ignores SIGTERM until killed. Its own
    process (not Steam's: they get the usual SIGTERM and can save) is killed
    shortly after the stop begins; if the unit still hasn't stopped after
    GAMESCOPE_STOP_GRACE, everything left in it is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GAMESCOPE_WATCH_TIMEOUT
    stopping_since = None
    killed = reported = False
    while loop.time() < deadline:
        _, state = await _systemctl_user("show", "-p", "ActiveState", "--value", GAMESCOPE_UNIT)
        if state in ("inactive", "failed"):
            if stopping_since is not None:
                decky.logger.info(f"{GAMESCOPE_UNIT} stopped after {loop.time() - stopping_since:.2f}s")
            return
        if state == "deactivating":
            stopping_since = stopping_since or loop.time()
            waited = loop.time() - stopping_since
            if not killed and waited >= GAMESCOPE_MAIN_GRACE:
                # The unit's main process is the gamescope-session script, so
                # find Gamescope itself among the unit's processes.
                pids = await _unit_processes(GAMESCOPE_UNIT)
                for pid, name in pids:
                    if name in GAMESCOPE_PROCESS_NAMES:
                        try:
                            os.kill(pid, SIGKILL)
                        except OSError:
                            pass
                decky.logger.info(f"stopping Gamescope; left in the session: {sorted({n for _, n in pids})}")
                killed = True
            if killed and not reported and waited >= 1:
                left = sorted({n for _, n in await _unit_processes(GAMESCOPE_UNIT)})
                decky.logger.info(f"{GAMESCOPE_UNIT} still stopping after 1 s: {left}")
                reported = True
            if waited >= GAMESCOPE_STOP_GRACE:
                decky.logger.info(f"{GAMESCOPE_UNIT} stuck stopping; sending SIGKILL to what's left")
                await _systemctl_user("kill", "--signal=SIGKILL", GAMESCOPE_UNIT)
                return
        await asyncio.sleep(0.1)


def build_direct_command(exe, launch_options):
    """Mirror how Steam combines a shortcut's exe and launch options."""
    exe = (exe or "").strip()
    if not exe:
        return None
    if not (len(exe) >= 2 and exe[0] == exe[-1] and exe[0] in "\"'"):
        exe = shlex.quote(exe)
    opts = (launch_options or "").strip()
    if "%command%" in opts:
        return opts.replace("%command%", exe)
    return f"{exe} {opts}".strip()


def _strip_quotes(value):
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class Plugin:
    def __init__(self):
        self.settings = dict(SETTINGS_DEFAULTS)
        self._gamescope_watchdog = None

    def _settings_path(self):
        return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

    def _load_settings(self):
        try:
            with open(self._settings_path()) as f:
                stored = json.load(f)
            self.settings = {**SETTINGS_DEFAULTS, **{k: v for k, v in stored.items() if k in SETTINGS_DEFAULTS}}
            # Before 0.4 this was a "performance" on/off toggle.
            if "power_profile" not in stored and stored.get("performance") is False:
                self.settings["power_profile"] = "battery"
        except FileNotFoundError:
            self.settings = dict(SETTINGS_DEFAULTS)
        except (OSError, ValueError) as e:
            decky.logger.warning(f"Could not read settings, using defaults: {e}")
            self.settings = dict(SETTINGS_DEFAULTS)
        # Ignore unknown per-app methods (e.g. from older versions).
        self.settings["launch_modes"] = {k: v for k, v in self.settings["launch_modes"].items() if v in LAUNCH_MODES}
        if self.settings["power_profile"] not in POWER_PROFILES:
            self.settings["power_profile"] = SETTINGS_DEFAULTS["power_profile"]

    def _save_settings(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        tmp = self._settings_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        os.replace(tmp, self._settings_path())

    async def _cleanup(self):
        """Drop the staged launch and undo every one-shot tweak."""
        _remove(_paths()["pending"])
        code, out = await _run_launcher("--restore")
        if code != 0:
            decky.logger.warning(f"launcher --restore failed: {out}")

    async def get_settings(self):
        return self.settings

    async def set_setting(self, key, value):
        if key not in SETTINGS_DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if key == "brightness_pct":
            value = max(5, min(100, int(value)))
        elif key == "display_scale":
            value = max(100, min(300, int(value)))
        elif key == "moonlight_profiles":
            value = {
                str(k): _clean_moonlight_profile(p) for k, p in value.items() if re.fullmatch(r"[\w.-]{1,40}", str(k))
            }
        elif key == "favorites":
            value = [int(v) for v in value]
        elif key == "pinned_streams":
            value = [{"host": str(p["host"]), "app": str(p["app"])} for p in value if p.get("host") and p.get("app")]
        elif key == "launch_modes":
            value = {str(int(k)): v for k, v in value.items() if v in LAUNCH_MODES}
        elif key == "power_profile" and value not in POWER_PROFILES:
            raise ValueError(f"Unknown power profile: {value}")
        elif key == "display_mode":
            value = str(value or "")
            if value and not re.fullmatch(r"\d+x\d+@\d+\.\d\d", value):
                raise ValueError(f"Bad display mode: {value}")
        elif isinstance(SETTINGS_DEFAULTS[key], bool):
            value = bool(value)
        self.settings[key] = value
        self._save_settings()
        return self.settings

    async def get_displays(self):
        """The display an app would use and the modes it offers, or None."""
        code, out = await _run_launcher("--displays")
        if code != 0:
            decky.logger.warning(f"launcher --displays failed: {out}")
            return None
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return None

    async def get_moonlight_settings(self):
        """Moonlight's own current values (read only), to start a new profile from."""
        code, out = await _run_launcher("--moonlight-settings")
        if code != 0:
            return None
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return None

    async def get_moonlight_hosts(self):
        """Moonlight's saved hosts and their apps: [{name, uuid, apps: [{id, name}]}]."""
        code, out = await _run_launcher("--moonlight-hosts")
        if code != 0:
            return []
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return []

    async def save_diagnostics(self):
        """Write a shareable report to ~/Downloads for bug reports. Returns its path."""
        code, report = await _run_launcher("--diagnostics")
        if code != 0:
            report = f"(launcher --diagnostics failed: {report})"
        settings = json.dumps(self.settings, indent=2, sort_keys=True)
        text = (
            f"Quickscope {decky.DECKY_PLUGIN_VERSION}\n"
            f"Decky Loader {getattr(decky, 'DECKY_VERSION', '?')}\n"
            f"{report}\n\n--- settings\n{settings}\n"
        )
        downloads = os.path.join(decky.DECKY_USER_HOME, "Downloads")
        os.makedirs(downloads, exist_ok=True)
        path = os.path.join(downloads, "quickscope-diagnostics.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    async def get_environment(self):
        return {
            "launcher_found": _launcher_source() is not None,
            "pending": os.path.exists(_paths()["pending"]),
            "wifi_backend": await _steamosctl_value("get-wifi-backend"),
            "wifi_powersave": await _steamosctl_value("get-wifi-power-management-state"),
            # The Flatpak or a native build: what stream entries need.
            "moonlight_installed": _moonlight_stream_command("host", "app") is not None,
        }

    async def prepare_launch(self, spec):
        """Stage a one-shot launch that the desktop session picks up on login."""
        try:
            appid = int(spec["appid"])
            name = str(spec.get("name") or appid)
            kind = spec.get("kind")
            if kind not in ("steam", "shortcut", "stream"):
                return {"ok": False, "error": f"Unknown app kind: {kind}"}
            if _install_launcher() is None:
                return {"ok": False, "error": "quickscope_launcher.py is missing from the plugin install"}

            mode, command, gameid, stream = "steam", None, None, None
            if kind == "stream":
                # Straight into a host app with Moonlight's `stream` command. Hybrid:
                # desktop Steam runs alongside for the controller layout.
                stream = {"host": str(spec["host"]), "app": str(spec["app"])}
                command = _moonlight_stream_command(stream["host"], stream["app"])
                if not command:
                    return {"ok": False, "error": "Moonlight isn't installed"}
                mode = DEFAULT_SHORTCUT_MODE
            elif kind == "shortcut":
                command = build_direct_command(spec.get("exe"), spec.get("launch_options"))
                if command:
                    mode = self.settings["launch_modes"].get(str(appid), DEFAULT_SHORTCUT_MODE)
                else:
                    # Only if Steam couldn't tell us the shortcut's exe;
                    # launching through Steam is slower but still works.
                    decky.logger.warning(f"No exe for shortcut {appid}; launching through Steam")
            if mode == "steam":
                gameid = (appid << 32) | SHORTCUT_GAMEID_FLAG if kind == "shortcut" else appid

            s = self.settings
            pending = {
                "version": 2,
                "created": time.time(),
                "appid": appid,
                "name": name,
                "mode": mode,
                "command": command,
                "cwd": _strip_quotes(spec.get("start_dir")) or None,
                "gameid": gameid,
                "return_to_gaming": s["return_to_gaming"],
                "force_fullscreen": s["force_fullscreen"],
                "power_profile": s["power_profile"],
                "wifi_powersave_off": s["wifi_powersave_off"],
                "wifi_lock_ap": s["wifi_lock_ap"],
                "lock_brightness": s["lock_brightness"],
                "match_gaming_brightness": s["match_gaming_brightness"],
                "brightness_pct": s["brightness_pct"],
                "display_mode": s["display_mode"],
                "display_hdr": s["display_hdr"],
                "display_scale": s["display_scale"],
                "moonlight_override": s["moonlight_override"],
                "moonlight_profiles": s["moonlight_profiles"],
                "stream": stream,
            }

            path = _paths()["pending"]
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(pending, f, indent=2)
            os.replace(tmp, path)

            code, out = await _run_launcher("--prepare")
            if code != 0:
                await self._cleanup()
                return {"ok": False, "error": out or f"launcher --prepare exited {code}"}

            decky.logger.info(f"Staged {mode} launch of {name} ({appid})")
            return {"ok": True, "mode": mode}
        except Exception as e:
            decky.logger.exception("prepare_launch failed")
            await self._cleanup()
            return {"ok": False, "error": str(e)}

    async def switch_session(self):
        """Switch out of Gaming Mode into Plasma on Wayland."""
        steamosctl = _which("steamosctl")
        if steamosctl:
            cmd = [steamosctl, "switch-to-desktop-mode", WAYLAND_SESSION]
        else:
            exe = _which("steamos-session-select")
            if exe is None:
                return {"ok": False, "error": "neither steamosctl nor steamos-session-select found"}
            cmd = [exe, "plasma-wayland"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=_system_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            return {"ok": False, "error": str(e)}
        self._gamescope_watchdog = asyncio.create_task(_hurry_gamescope_stop())
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            # Still running means the session teardown is underway.
            return {"ok": True}
        if proc.returncode != 0:
            self._gamescope_watchdog.cancel()
            msg = out.decode(errors="replace").strip() or f"exit code {proc.returncode}"
            return {"ok": False, "error": msg}
        return {"ok": True}

    async def cancel_pending(self):
        await self._cleanup()

    async def _main(self):
        self._load_settings()
        try:
            _install_launcher()
        except OSError as e:
            decky.logger.warning(f"Could not install launcher: {e}")
        # Undo leftovers from an interrupted launch, unless a Quickscope
        # session is running right now (Decky restarted underneath it): its
        # tweaks are live and the launcher undoes them when the app exits.
        _, unit_state = await _systemctl_user("is-active", LAUNCH_UNIT)
        if unit_state != "active" and not _pending_is_fresh():
            await self._cleanup()
        decky.logger.info("Quickscope loaded")

    async def _unload(self):
        decky.logger.info("Quickscope unloaded")

    async def _uninstall(self):
        code, out = await _run_launcher("--uninstall")
        if code != 0:
            decky.logger.warning(f"launcher --uninstall failed: {out}")
