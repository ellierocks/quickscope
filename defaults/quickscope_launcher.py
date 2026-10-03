#!/usr/bin/env python3
"""Quickscope launcher.

With no arguments it runs inside the desktop session, started by a systemd
user unit hooked into plasma-core.target (with a KDE autostart entry as a
fallback). It consumes the staged launch, runs the app outside Gamescope and
returns to Gaming Mode when the app exits.

  --prepare    install the unit and apply one-shot session tweaks for the
               staged launch (run by the Decky backend just before switching)
  --restore    undo any one-shot tweaks
  --uninstall  undo tweaks and remove the systemd unit

Every tweak lives in ~/.config or /run and is recorded in undo.json, so
nothing here touches files a SteamOS update replaces.

Standard library only: this runs on the system Python, not Decky's.
"""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".local", "state", "quickscope")
PENDING = os.path.join(STATE, "pending.json")
UNDO = os.path.join(STATE, "undo.json")
LOG = os.path.join(STATE, "launcher.log")
KWIN_SCRIPT = os.path.join(STATE, "session.js")
OSD_SCREEN = os.path.join(STATE, "osd.qml")
BACKLIGHT_ROOT = "/sys/class/backlight"
OSD_STATE = os.path.join(STATE, "osd.json")
LOADING_SCREEN = os.path.join(STATE, "loading.qml")
SELF = os.path.abspath(__file__)

AUTOSTART_DIR = os.path.join(HOME, ".config", "autostart")
KSPLASHRC = os.path.join(HOME, ".config", "ksplashrc")
ONESHOT_AUTOSTART = os.path.join(AUTOSTART_DIR, "quickscope-oneshot.desktop")
UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")
UNIT_NAME = "quickscope-launch.service"
UNIT_TARGET = "plasma-core.target"
PLASMASHELL_UNIT = "plasma-plasmashell.service"
KWIN_SCRIPT_NAME = "quickscope-session"
STEAM_FAST_START_ARGS = ["-silent", "-noverifyfiles", "-skipinitialbootstrap", "-norepairfiles"]
STEAMOS_CLIENT = "/usr/lib/steam/steam"
HYBRID_STEAM_DELAY = 1
# How long to wait for desktop Steam to exit cleanly before returning anyway.
STEAM_SHUTDOWN_TIMEOUT = 8
KEEPER_INTERVAL = 2

# Steam's brightness slider drives the backlight through roughly this curve
# (measured on a Deck LCD: 48% -> 26% of the backlight's range, 100% -> max).
BRIGHTNESS_EXPONENT = 1.84

# Quiet session: KDE's own background helpers are skipped for the launch
# session. The user's own autostart apps and services (sync clients keeping
# game saves in sync, etc.) are deliberately left alone.
QUIET_AUTOSTART = {
    "baloo_file.desktop", "org.kde.discover.notifier.desktop", "org.kde.kdeconnect.daemon.desktop",
    "print-applet.desktop", "orca-autostart.desktop",
}
QUIET_MASKED_UNITS = ["kde-baloo.service"]

PENDING_MAX_AGE = 300
KWIN_TIMEOUT = 20
# Roughly the last 10-15 launches.
LOG_KEEP_LINES = 400
# Steam may need to start, sign in or update before the game appears.
STEAM_APPEAR_TIMEOUT = 300
POLL_INTERVAL = 2
# Consecutive polls without the game before it's considered closed.
GONE_POLLS = 3

UNIT_TEMPLATE = """\
[Unit]
Description=Quickscope one-shot launch
After=plasma-kwin_x11.service plasma-kwin_wayland.service
PartOf=graphical-session.target
ConditionPathExists={pending}

[Service]
ExecStart="{python}" "{launcher}"
Slice=app.slice
# Steam may be started from here; don't take it down when the launcher exits.
KillMode=process

[Install]
WantedBy={target}
"""

AUTOSTART_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Quickscope Launcher
Exec="{python}" "{launcher}"
NoDisplay=true
OnlyShowIn=KDE;
"""

HIDDEN_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Hidden by Quickscope
Hidden=true
"""

# KWin script: when the app's first real window opens, fullscreen and focus it,
# close the loading screen, then disconnect.
SESSION_JS = """\
// Steam on Wayland triggers a screen-share portal dialog at startup; never
// fullscreen that or any other system prompt.
var ignored = ["steam", "steamwebhelper", "plasmashell", "org.kde.plasmashell",
               "krunner", "org.kde.krunner", "ksplashqml", "xwaylandvideobridge",
               "polkit-kde-authentication-agent-1", "org.kde.polkit-kde-authentication-agent-1",
               "org.freedesktop.impl.portal.desktop.kde", "xdg-desktop-portal-kde",
               "org.kde.kwalletd6", "kwalletd6", "org.kde.drkonqi"];
var forceFullscreen = %FORCE_FULLSCREEN%;
var loadingPid = %LOADING_PID%;
var windowAdded = workspace.windowAdded || workspace.clientAdded;
function isLoadingScreen(w) {
    return w.pid === loadingPid || String(w.caption) === "%LOADING_TITLE%";
}
function isOwnOverlay(w) {
    return isLoadingScreen(w) || String(w.caption) === "%OSD_TITLE%";
}
function closeLoadingScreen() {
    var ws = workspace.windowList ? workspace.windowList() : workspace.clientList();
    for (var i = 0; i < ws.length; i++) {
        if (isLoadingScreen(ws[i])) ws[i].closeWindow();
    }
}
function onWindowAdded(w) {
    if (!w || !w.normalWindow || isOwnOverlay(w)) return;
    if (ignored.indexOf(String(w.resourceClass).toLowerCase()) !== -1) return;
    windowAdded.disconnect(onWindowAdded);
    if (forceFullscreen) w.fullScreen = true;
    if ("activeWindow" in workspace) workspace.activeWindow = w;
    else workspace.activeClient = w;
    // The app's window is up; the loading screen has done its job.
    closeLoadingScreen();
}
windowAdded.connect(onWindowAdded);
"""

LOADING_TITLE = "Quickscope Loading"
LOADING_QML = """\
import QtQuick
import QtQuick.Controls
import QtQuick.Window

Window {
    title: %(title)s
    visible: true
    visibility: Window.FullScreen
    color: "black"

    Column {
        anchors.centerIn: parent
        spacing: 24
        BusyIndicator {
            running: true
            anchors.horizontalCenter: parent.horizontalCenter
        }
        Text {
            text: %(message)s
            color: "#d0d0d0"
            font.pixelSize: 26
            anchors.horizontalCenter: parent.horizontalCenter
        }
    }
}
"""
OSD_TITLE = "Quickscope OSD"
# Volume indicator. Drawn as a layer-shell overlay (QT_WAYLAND_SHELL_INTEGRATION
# =layer-shell) so it shows above fullscreen apps without taking focus; the
# launcher writes the volume to a small JSON file that this polls.
OSD_QML = """\
import QtQuick
import org.kde.layershell 1.0 as LayerShell

Window {
    id: osd
    title: %(title)s
    width: 360
    height: 120
    visible: false
    color: "transparent"
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "quickscope-osd"

    property int seq: 0
    property int volume: 0
    property bool muted: false

    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 48
        width: parent.width
        height: 56
        radius: 10
        color: "#e6171a21"

        Row {
            anchors.centerIn: parent
            spacing: 16
            Text {
                text: "Volume"
                color: "#dcdedf"
                font.pixelSize: 20
                anchors.verticalCenter: parent.verticalCenter
            }
            Rectangle {
                width: 160
                height: 8
                radius: 4
                color: "#3d4450"
                anchors.verticalCenter: parent.verticalCenter
                Rectangle {
                    width: osd.muted ? 0 : parent.width * Math.min(osd.volume, 100) / 100
                    height: parent.height
                    radius: 4
                    color: "#1a9fff"
                }
            }
            Text {
                text: osd.muted ? "Muted" : osd.volume + "%%"
                color: "#ffffff"
                font.pixelSize: 20
                width: 64
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Timer { id: hide; interval: 1500; onTriggered: osd.visible = false }
    Timer {
        interval: 100
        running: true
        repeat: true
        onTriggered: {
            try {
                var xhr = new XMLHttpRequest();
                xhr.open("GET", %(state_url)s, false);
                xhr.send();
                var s = JSON.parse(xhr.responseText);
                if (s.seq === osd.seq) return;
                osd.seq = s.seq;
                osd.volume = s.volume;
                osd.muted = s.muted;
                osd.visible = true;
                hide.restart();
            } catch (e) {}
        }
    }
}
"""
# Without the KWin script: keep the loading screen up briefly after a Steam
# game starts so its window is on top first, and for direct launches give up
# after a fixed time.
LOADING_LINGER = 3
LOADING_FALLBACK = 6


# --- helpers -----------------------------------------------------------------

def log(msg):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def trim_log(keep=LOG_KEEP_LINES):
    """Keep only the most recent lines of the log (it's appended on every launch)."""
    try:
        with open(LOG) as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(LOG, "w") as f:
                f.writelines(lines[-keep:])
    except OSError:
        pass


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log(f"unreadable {path}: {e}")
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        log(f"{cmd[0]}: {e}")
        return None


def succeeded(result):
    return result is not None and result.returncode == 0


def systemctl(*args):
    return run(["systemctl", "--user", *args])


def dbus(dest, path, method, *args):
    result = run(["dbus-send", "--session", "--print-reply", f"--dest={dest}", "--type=method_call", path, method, *args])
    return result.stdout if succeeded(result) else None


def python_exe():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


class SessionKeeper(threading.Thread):
    """Re-applies session settings that something else resets while the app
    runs: desktop Steam puts the GPU performance level back to auto when it
    starts, and KDE's power management changes the brightness."""

    def __init__(self, pin_gpu, brightness_raw):
        super().__init__(daemon=True)
        self.pin_gpu = pin_gpu
        self.brightness_raw = brightness_raw
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(KEEPER_INTERVAL):
            if self.pin_gpu:
                level = run(["steamosctl", "get-gpu-performance-level"])
                if succeeded(level) and level.stdout.rsplit(":", 1)[-1].strip() != "high":
                    if succeeded(run(["steamosctl", "set-gpu-performance-level", "high"])):
                        log(f"GPU performance level was reset to {level.stdout.rsplit(':', 1)[-1].strip()}, pinned high again")
            if self.brightness_raw is not None:
                current = read_backlight()
                if current and current[0] != self.brightness_raw and write_backlight(self.brightness_raw):
                    log(f"brightness was changed to {current[0]}, locked back to {self.brightness_raw}")

    def stop(self):
        # Wait so a check in flight can't re-apply after restore() resets things.
        self.stopped.set()
        self.join(timeout=5)


def backlight_dir():
    dirs = sorted(glob.glob(os.path.join(BACKLIGHT_ROOT, "*")))
    return dirs[0] if dirs else None


def read_backlight():
    """(brightness, max_brightness) of the first backlight, or None."""
    d = backlight_dir()
    if not d:
        return None
    try:
        with open(os.path.join(d, "brightness")) as f, open(os.path.join(d, "max_brightness")) as m:
            return int(f.read().strip()), int(m.read().strip())
    except (OSError, ValueError):
        return None


def write_backlight(raw):
    d = backlight_dir()
    if not d:
        return False
    try:
        with open(os.path.join(d, "brightness"), "w") as f:
            f.write(str(int(raw)))
        return True
    except OSError as e:
        log(f"could not set brightness: {e}")
        return False


def brightness_pct_to_raw(pct, max_raw):
    """Map a Steam-style brightness percentage to a raw backlight value."""
    pct = max(1, min(100, pct))
    return max(1, round(max_raw * (pct / 100) ** BRIGHTNESS_EXPONENT))


def parse_volume(volume_out, mute_out):
    """(percent, muted) from `pactl get-sink-volume` / `get-sink-mute` output."""
    m = re.search(r"(\d+)%", volume_out or "")
    if not m:
        return None
    return int(m.group(1)), "yes" in (mute_out or "").lower()


def read_volume():
    vol = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    mute = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    if not (succeeded(vol) and succeeded(mute)):
        return None
    return parse_volume(vol.stdout, mute.stdout)


class VolumeOsd:
    """Shows a volume indicator when the volume changes. Without plasmashell
    nothing else draws one."""

    def __init__(self):
        self.osd = None
        self.watch = None
        self.seq = 0
        self.last = None

    def start(self):
        qml = shutil.which("qml6") or shutil.which("qml")
        if not (qml and shutil.which("pactl")):
            log("no qml runtime or pactl, skipping the volume indicator")
            return False
        write_file(OSD_SCREEN, OSD_QML % {
            "title": json.dumps(OSD_TITLE),
            "state_url": json.dumps("file://" + OSD_STATE),
        })
        self.last = read_volume()
        self._write()
        env = dict(os.environ, QT_WAYLAND_SHELL_INTEGRATION="layer-shell", QML_XHR_ALLOW_FILE_READ="1")
        try:
            self.osd = subprocess.Popen([qml, OSD_SCREEN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.watch = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log(f"volume indicator failed to start: {e}")
            self.stop()
            return False
        threading.Thread(target=self._follow, daemon=True).start()
        log("volume indicator running")
        return True

    def _write(self):
        volume, muted = self.last or (0, False)
        write_json(OSD_STATE, {"seq": self.seq, "volume": volume, "muted": muted})

    def _follow(self):
        for line in self.watch.stdout:
            # One volume change produces several events; only show real changes.
            if "on sink" not in line and "on server" not in line:
                continue
            current = read_volume()
            if current and current != self.last:
                self.last = current
                self.seq += 1
                self._write()

    def stop(self):
        for proc in (self.watch, self.osd):
            if proc and proc.poll() is None:
                proc.terminate()


def boost_performance():
    """Pin GPU clocks high and use the performance CPU governor for the
    session. Returns whether the GPU level was pinned (and needs keeping)."""
    if not shutil.which("steamosctl"):
        return False
    before = {}
    gpu = run(["steamosctl", "get-gpu-performance-level"])
    governor = run(["steamosctl", "get-cpu-scaling-governor"])
    if succeeded(gpu) and succeeded(run(["steamosctl", "set-gpu-performance-level", "high"])):
        before["gpu"] = gpu.stdout.rsplit(":", 1)[-1].strip()
    if succeeded(governor) and succeeded(run(["steamosctl", "set-cpu-scaling-governor", "performance"])):
        before["governor"] = governor.stdout.rsplit(":", 1)[-1].strip()
    if before:
        record_undo("performance", before)
        log(f"performance mode on (was {before})")
    return "gpu" in before


# --- undo bookkeeping --------------------------------------------------------

def record_undo(key, value):
    undo = read_json(UNDO) or {}
    undo[key] = value
    write_json(UNDO, undo)


def restore(reload=True, restart_shell=False):
    """Undo every one-shot tweak recorded in undo.json. Safe to run repeatedly."""
    undo = read_json(UNDO) or {}
    remove(UNDO)
    for entry in undo.get("hidden_autostart", []):
        remove(entry["path"])
        if entry.get("backup") and os.path.exists(entry["backup"]):
            os.replace(entry["backup"], entry["path"])
        log(f"restored {entry['path']}")
    for unit in undo.get("masked", []):
        if not succeeded(systemctl("unmask", "--runtime", unit)):
            log(f"failed to unmask {unit}")
        elif restart_shell and unit == PLASMASHELL_UNIT:
            systemctl("start", "--no-block", unit)
    if "splash_engine" in undo:
        restore_splash(undo["splash_engine"])
    if "brightness" in undo and write_backlight(undo["brightness"]["raw"]):
        log(f"restored brightness {undo['brightness']['raw']}")
    perf = undo.get("performance", {})
    if perf.get("gpu"):
        run(["steamosctl", "set-gpu-performance-level", perf["gpu"]])
    if perf.get("governor"):
        run(["steamosctl", "set-cpu-scaling-governor", perf["governor"]])
    if perf:
        log(f"restored performance settings {perf}")
    remove(ONESHOT_AUTOSTART)
    if reload:
        systemctl("daemon-reload")


# --- prepare (Gaming Mode, before the switch) --------------------------------

def install_unit():
    unit_path = os.path.join(UNIT_DIR, UNIT_NAME)
    write_file(unit_path, UNIT_TEMPLATE.format(pending=PENDING, python=python_exe(), launcher=SELF, target=UNIT_TARGET))
    link = os.path.join(UNIT_DIR, f"{UNIT_TARGET}.wants", UNIT_NAME)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(unit_path, link)


def read_desktop_entry(path):
    entry, in_section = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_section = line == "[Desktop Entry]"
        elif in_section and "=" in line:
            key, value = line.split("=", 1)
            entry.setdefault(key.strip(), value.strip())
    return entry


def is_steam_entry(entry):
    try:
        argv = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return False
    return bool(argv) and (os.path.basename(argv[0]) == "steam" or "com.valvesoftware.Steam" in argv)


def hide_autostart(should_hide):
    """Shadow matching autostart entries with Hidden=true user overrides.

    should_hide(name, entry, is_user_entry) decides per entry.
    """
    config_dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    search = [AUTOSTART_DIR] + [os.path.join(d, "autostart") for d in config_dirs.split(":") if d]
    names = {os.path.basename(p) for d in search for p in glob.glob(os.path.join(d, "*.desktop"))}
    names.discard(os.path.basename(ONESHOT_AUTOSTART))
    hidden = []
    for name in sorted(names):
        # User entries shadow system entries with the same file name.
        effective = next((p for p in (os.path.join(d, name) for d in search) if os.path.exists(p)), None)
        entry = read_desktop_entry(effective) if effective else None
        if not entry or entry.get("Hidden", "").lower() == "true":
            continue
        if not should_hide(name, entry, os.path.dirname(effective) == AUTOSTART_DIR):
            continue
        user_path = os.path.join(AUTOSTART_DIR, name)
        backup = None
        if os.path.exists(user_path):
            backup = user_path + ".quickscope-bak"
            os.replace(user_path, backup)
        write_file(user_path, HIDDEN_ENTRY)
        hidden.append({"path": user_path, "backup": backup})
        # Record as we go so a failure part-way through is still undoable.
        record_undo("hidden_autostart", hidden)
        log(f"hid autostart {effective}")
    return hidden


def kconfig_tool(kind):
    """kreadconfig6/kwriteconfig6 (or the Plasma 5 names)."""
    return shutil.which(f"k{kind}config6") or shutil.which(f"k{kind}config5")


def disable_splash():
    """Turn the Plasma splash off for this session only.

    Plasma starts the splash over D-Bus activation, not through
    plasma-ksplash.service, so masking that unit doesn't stop it. KDE's own
    switch is ksplashrc's Engine=none; the original value is restored after.
    """
    read, write = kconfig_tool("read"), kconfig_tool("write")
    if not (read and write):
        log("kreadconfig/kwriteconfig not found, leaving the splash on")
        return
    current = run([read, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine"])
    if not succeeded(current):
        return
    original = current.stdout.strip() or None  # None: key wasn't set
    if original == "none":
        return
    file_existed = os.path.exists(KSPLASHRC)
    if succeeded(run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", "none"])):
        record_undo("splash_engine", {"value": original, "file_existed": file_existed})
        log(f"splash off for this session (Engine was {original!r})")


def restore_splash(undo):
    original = undo.get("value")
    if not undo.get("file_existed", True):
        # We created ksplashrc just for this session.
        remove(KSPLASHRC)
        log("removed session-only ksplashrc")
        return
    write = kconfig_tool("write")
    if not write:
        return
    if original is None:
        run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", "--delete"])
    else:
        run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", original])
    log(f"restored splash Engine {original!r}")


def lock_brightness(pending):
    """Decide the session's brightness while still in Gaming Mode, where the
    backlight is at the level the user chose there."""
    current = read_backlight()
    if not current:
        log("no backlight found, not locking brightness")
        return
    raw, max_raw = current
    if pending.get("match_gaming_brightness", True):
        target = raw
    else:
        target = brightness_pct_to_raw(pending.get("brightness_pct", 50), max_raw)
    pending["brightness_raw"] = target
    write_json(PENDING, pending)
    record_undo("brightness", {"raw": raw})
    log(f"brightness locked at {target} (Gaming Mode had {raw} of {max_raw})")


def prepare():
    trim_log()
    restore(reload=False)
    pending = read_json(PENDING)
    if pending is None:
        print("no staged launch")
        return 1

    install_unit()
    write_file(ONESHOT_AUTOSTART, AUTOSTART_TEMPLATE.format(python=python_exe(), launcher=SELF))

    # Every launch method either starts Steam itself (Steam games, hybrid) or
    # deliberately runs without it (direct), so desktop Steam's own autostart
    # is never wanted: it would race our Steam or grab the controller.
    quiet = pending.get("quiet_session", True)
    hide_autostart(lambda name, entry, is_user: (
        is_steam_entry(entry)
        or (quiet and not is_user and name in QUIET_AUTOSTART)
    ))

    # The splash waits for the Plasma panel, so with the minimal desktop it
    # would sit there until it times out.
    if pending.get("skip_splash") or pending.get("minimal_desktop"):
        disable_splash()

    if pending.get("lock_brightness"):
        lock_brightness(pending)

    to_mask = []
    if pending.get("minimal_desktop"):
        to_mask.append(PLASMASHELL_UNIT)
    if quiet:
        to_mask.extend(QUIET_MASKED_UNITS)
    masked = []
    for unit in to_mask:
        if succeeded(systemctl("mask", "--runtime", unit)):
            masked.append(unit)
            record_undo("masked", masked)
            log(f"masked {unit} for this session")
        else:
            log(f"failed to mask {unit}")

    reload = systemctl("daemon-reload")
    if not succeeded(reload):
        print(f"systemctl --user daemon-reload failed: {reload.stdout.strip() if reload else 'not found'}")
        return 1
    log(f"prepared {pending.get('name')} ({pending.get('mode')})")
    return 0


# --- launch (inside the desktop session) -------------------------------------

def claim_pending():
    """Atomically take the staged launch so the unit and autostart can't both run it."""
    claimed = f"{PENDING}.{os.getpid()}"
    try:
        os.rename(PENDING, claimed)
    except FileNotFoundError:
        return None
    remove(ONESHOT_AUTOSTART)
    try:
        pending = read_json(claimed)
    finally:
        remove(claimed)
    if pending is None:
        return None
    age = time.time() - pending.get("created", 0)
    if age > PENDING_MAX_AGE:
        log(f"ignoring stale launch ({int(age)}s old)")
        restore()
        return None
    return pending


def wait_for_kwin():
    deadline = time.monotonic() + KWIN_TIMEOUT
    while time.monotonic() < deadline:
        out = dbus("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner", "string:org.kde.KWin")
        if out and "boolean true" in out:
            return True
        time.sleep(0.1)
    return False


def load_session_script(force_fullscreen, loading):
    """Load the KWin script that fullscreens/activates the app's first window
    and closes the loading screen once that window appears."""
    script = (SESSION_JS
              .replace("%LOADING_TITLE%", LOADING_TITLE)
              .replace("%OSD_TITLE%", OSD_TITLE)
              .replace("%LOADING_PID%", str(loading.pid if loading else -1))
              .replace("%FORCE_FULLSCREEN%", "true" if force_fullscreen else "false"))
    write_file(KWIN_SCRIPT, script)
    dbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{KWIN_SCRIPT_NAME}")
    out = dbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript",
               f"string:{KWIN_SCRIPT}", f"string:{KWIN_SCRIPT_NAME}")
    match = re.search(r"int32 (-?\d+)", out or "")
    if not match or int(match.group(1)) < 0:
        log("could not load KWin session script")
        return False
    script_id = match.group(1)
    # Plasma 6 path first, Plasma 5 second.
    for path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
        if dbus("org.kde.KWin", path, "org.kde.kwin.Script.run") is not None:
            log("KWin session script running")
            return True
    log("could not run KWin session script")
    return False


def show_loading_screen(message):
    """A full-screen loading screen with `message`. Returns the process or None."""
    qml = shutil.which("qml6") or shutil.which("qml")
    if not qml:
        log("no qml runtime, skipping loading screen")
        return None
    write_file(LOADING_SCREEN, LOADING_QML % {
        "title": json.dumps(LOADING_TITLE),
        "message": json.dumps(message),
    })
    try:
        return subprocess.Popen([qml, LOADING_SCREEN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"loading screen failed: {e}")
        return None


def close_loading_screen(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def unload_session_script():
    dbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{KWIN_SCRIPT_NAME}")


def steam_game_running(appid):
    """Steam wraps every launch (games and shortcuts) in `reaper SteamLaunch AppId=N`."""
    needle = f"AppId={appid}".encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if b"SteamLaunch" in args and needle in args:
            return True
    return False


def run_direct(pending, background_steam=False):
    cwd = pending.get("cwd")
    if cwd and not os.path.isdir(cwd):
        log(f"start dir {cwd!r} missing, using home")
        cwd = None
    log(f"exec: {pending['command']}")
    started = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", pending["command"]], cwd=cwd or HOME)
    if background_steam:
        # Give the app a head start on CPU and disk before Steam's heavy startup.
        time.sleep(HYBRID_STEAM_DELAY)
        start_background_steam()
    code = proc.wait()
    log(f"exited with {code} after {int(time.monotonic() - started)}s")


def steam_command(url=None):
    # The fast-start flags skip the install checksum Steam runs after Gaming
    # Mode's Steam is killed; -silent keeps its window from covering the game.
    wrapper = shutil.which("steam")
    if wrapper and os.path.realpath(wrapper).endswith("steam-jupiter") and os.path.exists(STEAMOS_CLIENT):
        # Bypass SteamOS's wrapper: its -pipewire flag triggers a screen-capture
        # prompt on Wayland, and its other setup already ran in Gaming Mode.
        cmd = [STEAMOS_CLIENT, "-steamdeck", *STEAM_FAST_START_ARGS]
    elif wrapper:
        cmd = [wrapper, *STEAM_FAST_START_ARGS]
    elif url:
        return ["xdg-open", url]
    else:
        return None
    return cmd + [url] if url else cmd


def start_background_steam():
    cmd = steam_command()
    if cmd is None:
        log("steam not found, can't start it in the background")
        return
    log(f"background: {' '.join(cmd)}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_via_steam(pending, loading=None):
    url = f"steam://rungameid/{pending['gameid']}"
    cmd = steam_command(url)
    log(f"launch: {' '.join(cmd)}")
    # Don't wait: if Steam isn't running yet, this process becomes Steam.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    appid = pending["appid"]
    deadline = time.monotonic() + STEAM_APPEAR_TIMEOUT
    while not steam_game_running(appid):
        if time.monotonic() > deadline:
            log(f"AppId={appid} never started")
            return
        # Poll quickly here so the loading screen closes promptly.
        time.sleep(0.5)
    log(f"AppId={appid} running")
    if loading is not None:
        time.sleep(LOADING_LINGER)
        close_loading_screen(loading)

    misses = 0
    while misses < GONE_POLLS:
        misses = 0 if steam_game_running(appid) else misses + 1
        time.sleep(POLL_INTERVAL)
    log(f"AppId={appid} exited")


def steam_running():
    return succeeded(run(["pgrep", "-x", "steam"]))


def request_steam_shutdown():
    """Ask desktop Steam to exit cleanly. Killed at logout, it would leave its
    crash marker behind and Gaming Mode's Steam would re-verify its install and
    run an update check. Returns when the request was sent, or None."""
    if not steam_running():
        return None
    client = STEAMOS_CLIENT if os.path.exists(STEAMOS_CLIENT) else shutil.which("steam")
    if not client:
        return None
    try:
        subprocess.Popen([client, "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        log(f"could not ask Steam to shut down: {e}")
        return None
    log("asked desktop Steam to shut down")
    return time.monotonic()


def wait_for_steam_exit(requested):
    while time.monotonic() - requested < STEAM_SHUTDOWN_TIMEOUT:
        if not steam_running():
            log(f"desktop Steam exited cleanly after {time.monotonic() - requested:.1f}s")
            return
        time.sleep(0.2)
    log("desktop Steam still running, returning anyway")


def return_to_gaming():
    exe = shutil.which("steamos-session-select")
    if exe:
        log("returning to Gaming Mode")
        if succeeded(run([exe, "gamescope"])):
            return
    # Fallback: SteamOS one-shot Plasma sessions return to Gaming Mode on logout.
    log("falling back to Plasma logout")
    dbus("org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown.logout")


def launch():
    started = time.monotonic()
    pending = claim_pending()
    if pending is None:
        return 0
    log(f"--- {pending.get('name')} ({pending.get('appid')}), mode={pending.get('mode')}, "
        f"session={os.environ.get('XDG_SESSION_TYPE', '?')}")

    # KDE's power management sets its own brightness as the desktop starts.
    brightness_raw = pending.get("brightness_raw")
    if brightness_raw is not None:
        write_backlight(brightness_raw)

    # Loading screen right away: KWin's Wayland socket exists as soon as its
    # service has started, before KWin answers on D-Bus.
    use_loading = pending.get("loading_screen", True)
    starting = f"Starting {pending.get('name') or 'game'}…"
    loading = show_loading_screen(starting) if use_loading else None

    if wait_for_kwin():
        log(f"KWin ready after {time.monotonic() - started:.1f}s")
    else:
        log("KWin never appeared on D-Bus, launching anyway")
    if loading and loading.poll() is not None:
        log("loading screen exited early, starting it again")
        loading = show_loading_screen(starting)

    script = bool((pending.get("force_fullscreen") or loading)
                  and load_session_script(pending.get("force_fullscreen"), loading))
    if loading and not script:
        # Without the script nothing can tell when the app's window is up.
        timer = threading.Timer(LOADING_FALLBACK, close_loading_screen, [loading])
        timer.daemon = True
        timer.start()

    pin_gpu = boost_performance() if pending.get("performance", True) else False
    keeper = None
    if pin_gpu or brightness_raw is not None:
        keeper = SessionKeeper(pin_gpu, brightness_raw)
        keeper.start()
    # Plasma's panel normally draws the volume indicator.
    osd = VolumeOsd() if pending.get("volume_osd", True) and pending.get("minimal_desktop") else None
    if osd and not osd.start():
        osd = None
    returning = pending.get("return_to_gaming")
    try:
        log(f"launching {time.monotonic() - started:.1f}s after start")
        if pending.get("mode") in ("direct", "hybrid"):
            run_direct(pending, background_steam=pending.get("mode") == "hybrid")
        else:
            # Steam's game process appearing is the fallback signal when the
            # KWin script couldn't load.
            run_via_steam(pending, None if script else loading)
    except Exception as e:
        log(f"launch failed: {e!r}")
    finally:
        close_loading_screen(loading)
        if returning and use_loading:
            # Covers the clean-up and logout; the logout closes it.
            show_loading_screen("Returning to Gaming Mode…")
        steam_shutdown = request_steam_shutdown() if returning else None
        if osd:
            osd.stop()
        if keeper:
            keeper.stop()
        if script:
            unload_session_script()
        # Undo before returning: the logout ends this process.
        restore(restart_shell=not returning)
        if steam_shutdown:
            wait_for_steam_exit(steam_shutdown)

    if returning:
        return_to_gaming()
    return 0


def uninstall():
    """Undo everything and remove every file Quickscope created."""
    restore(reload=False)
    remove(os.path.join(UNIT_DIR, f"{UNIT_TARGET}.wants", UNIT_NAME))
    remove(os.path.join(UNIT_DIR, UNIT_NAME))
    systemctl("daemon-reload")
    # Includes this script's own copy, the log and generated files.
    shutil.rmtree(STATE, ignore_errors=True)


def main(argv):
    command = argv[1] if len(argv) > 1 else None
    if command == "--prepare":
        return prepare()
    if command == "--restore":
        restore()
        return 0
    if command == "--uninstall":
        uninstall()
        return 0
    return launch()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
