import asyncio
import json
import os
import pwd
import shlex
import shutil
import time

import decky

SETTINGS_DEFAULTS = {
    # Return to Gaming Mode once the launched app exits.
    "return_to_gaming": True,
    # Suspend KWin compositing while the app runs (X11 only; Wayland KWin
    # already does direct scanout for fullscreen windows).
    "suspend_compositor": True,
    # Fullscreen the app's first window via a temporary KWin script.
    "force_fullscreen": True,
    # Skip the Plasma splash screen for the launch session.
    "skip_splash": True,
    # Skip plasmashell (panel/desktop) entirely for the launch session.
    "minimal_desktop": True,
    # Show a fullscreen loading screen until the app's window appears.
    "loading_screen": True,
    # "auto" uses the system's default desktop session (steamosctl
    # get-default-desktop-session); "plasma" forces X11, "plasma-wayland"
    # forces Wayland.
    "desktop_session": "auto",
    # Extra seconds to wait after KWin is ready before launching.
    "launch_delay": 0,
    "favorites": [],
    # Per-shortcut launch method: {"<appid>": "direct"}. Shortcuts default to
    # "hybrid": launched directly with desktop Steam started alongside, so
    # Steam's desktop layout takes over the controller once it's up. "direct"
    # is the fallback with no Steam at all. (Steam games always launch
    # through Steam; nothing else can start them.)
    "launch_modes": {},
}

DESKTOP_SESSIONS = ("auto", "plasma", "plasma-wayland")
# steamosctl switch-to-desktop-mode arguments for each setting.
STEAMOSCTL_SESSIONS = {
    "auto": [],
    "plasma": ["plasmax11.desktop"],
    "plasma-wayland": ["plasma.desktop"],
}
LAUNCH_MODES = ("hybrid", "direct")
DEFAULT_SHORTCUT_MODE = "hybrid"

# Must match PENDING_MAX_AGE in quickscope_launcher.py.
PENDING_MAX_AGE = 300

SHORTCUT_GAMEID_FLAG = 0x02000000

LAUNCHER_TIMEOUT = 30

GAMESCOPE_UNIT = "gamescope-session.service"
# Normal Gamescope stops take ~1-2.2 s on the Deck; its unit's own timeout is 10 s.
GAMESCOPE_STOP_GRACE = 3
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
        "log": os.path.join(state, "launcher.log"),
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


async def _run_launcher(*args):
    launcher = _paths()["launcher"]
    if not os.path.isfile(launcher):
        return 1, "launcher not installed"
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else (_which("python3") or "python3")
    proc = await asyncio.create_subprocess_exec(
        python, launcher, *args,
        env=_system_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=LAUNCHER_TIMEOUT)
    except asyncio.TimeoutError:
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
        "systemctl", "--user", *args,
        env=_system_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def _hurry_gamescope_stop():
    """Gamescope sometimes ignores SIGTERM while leaving Gaming Mode and sits
    out its whole stop timeout before systemd SIGKILLs it. Send that SIGKILL
    as soon as it's clearly stuck instead."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + GAMESCOPE_WATCH_TIMEOUT
    stopping_since = None
    while loop.time() < deadline:
        _, state = await _systemctl_user("show", "-p", "ActiveState", "--value", GAMESCOPE_UNIT)
        if state in ("inactive", "failed"):
            return
        if state == "deactivating":
            stopping_since = stopping_since or loop.time()
            if loop.time() - stopping_since >= GAMESCOPE_STOP_GRACE:
                decky.logger.info(f"{GAMESCOPE_UNIT} stuck stopping; sending SIGKILL early")
                await _systemctl_user("kill", "--signal=SIGKILL", GAMESCOPE_UNIT)
                return
        await asyncio.sleep(0.25)


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
        except FileNotFoundError:
            self.settings = dict(SETTINGS_DEFAULTS)
        except (OSError, ValueError) as e:
            decky.logger.warning(f"Could not read settings, using defaults: {e}")
            self.settings = dict(SETTINGS_DEFAULTS)
        if self.settings["desktop_session"] not in DESKTOP_SESSIONS:
            self.settings["desktop_session"] = "auto"
        # Drops the removed "steam" per-app method; those apps get the default.
        self.settings["launch_modes"] = {
            k: v for k, v in self.settings["launch_modes"].items() if v in LAUNCH_MODES}

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
        if key == "desktop_session" and value not in DESKTOP_SESSIONS:
            raise ValueError(f"Unknown desktop session: {value}")
        if key == "launch_delay":
            value = max(0, min(15, int(value)))
        elif key == "favorites":
            value = [int(v) for v in value]
        elif key == "launch_modes":
            value = {str(int(k)): v for k, v in value.items() if v in LAUNCH_MODES}
        elif isinstance(SETTINGS_DEFAULTS[key], bool):
            value = bool(value)
        self.settings[key] = value
        self._save_settings()
        return self.settings

    async def get_environment(self):
        return {
            "session_select": (_which("steamosctl") or _which("steamos-session-select")) is not None,
            "launcher_found": _launcher_source() is not None,
            "pending": os.path.exists(_paths()["pending"]),
            "log_path": _paths()["log"],
        }

    async def prepare_launch(self, spec):
        """Stage a one-shot launch that the desktop session picks up on login."""
        try:
            appid = int(spec["appid"])
            name = str(spec.get("name") or appid)
            kind = spec.get("kind")
            if kind not in ("steam", "shortcut"):
                return {"ok": False, "error": f"Unknown app kind: {kind}"}
            if _install_launcher() is None:
                return {"ok": False, "error": "quickscope_launcher.py is missing from the plugin install"}

            mode, command, gameid = "steam", None, None
            if kind == "shortcut":
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
                "suspend_compositor": s["suspend_compositor"],
                "force_fullscreen": s["force_fullscreen"],
                "skip_splash": s["skip_splash"],
                "minimal_desktop": s["minimal_desktop"],
                "loading_screen": s["loading_screen"],
                # Opinionated session tuning; Quickscope owns this session.
                "quiet_session": True,
                "performance": True,
                "app_tuning": True,
                "launch_delay": s["launch_delay"],
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

    async def switch_session(self, session):
        """Switch out of Gaming Mode into the chosen (or default) desktop session."""
        if session not in DESKTOP_SESSIONS:
            session = "auto"
        steamosctl = _which("steamosctl")
        if steamosctl:
            # No session argument means the system's default desktop session.
            cmd = [steamosctl, "switch-to-desktop-mode", *STEAMOSCTL_SESSIONS[session]]
        else:
            exe = _which("steamos-session-select")
            if exe is None:
                return {"ok": False, "error": "neither steamosctl nor steamos-session-select found"}
            cmd = [exe, "plasma-wayland" if session == "plasma-wayland" else "plasma"]
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
        except asyncio.TimeoutError:
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
        if not _pending_is_fresh():
            await self._cleanup()
        decky.logger.info("Quickscope loaded")

    async def _unload(self):
        decky.logger.info("Quickscope unloaded")

    async def _uninstall(self):
        code, out = await _run_launcher("--uninstall")
        if code != 0:
            decky.logger.warning(f"launcher --uninstall failed: {out}")
