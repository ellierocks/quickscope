#!/usr/bin/env python3
"""Quickscope launcher.

With no arguments it runs inside the desktop session, started by a systemd
user unit hooked into plasma-core.target (with a KDE autostart entry as a
fallback). It consumes the staged launch, runs the app outside Gamescope and
returns to Gaming Mode when the app exits.

  --prepare    install the unit and apply one-shot session tweaks for the
               staged launch (run by the Decky backend just before switching)
  --restore    undo any one-shot tweaks
  --displays   print the display an app would use and its modes, as JSON
  --moonlight-settings
               print Moonlight's current resolution, frame rate and sync settings
  --uninstall  undo tweaks and remove the systemd unit

Every tweak lives in ~/.config or /run and is recorded in undo.json, so
nothing here touches files a SteamOS update replaces.

Standard library only: this runs on the system Python, not Decky's.
"""
import glob
import json
import os
import re
import select
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
KWIN_OUTPUT_CONFIG = os.path.join(HOME, ".config", "kwinoutputconfig.json")
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

# steamosctl (get, set) commands for each power setting.
POWER_SETTINGS = {
    "gpu": ("get-gpu-performance-level", "set-gpu-performance-level"),
    "governor": ("get-cpu-scaling-governor", "set-cpu-scaling-governor"),
    "boost": ("get-cpu-boost-state", "set-cpu-boost-state"),
}
# Measured streaming over Moonlight (APU power): SteamOS defaults 7.8 W,
# performance 8.5 W, battery saver 4.8 W. CPU boost is most of the difference;
# a TDP limit saved nothing more, the stream already draws under 6 W.
POWER_PROFILES = {
    "performance": {"gpu": "high", "governor": "performance", "boost": "enabled"},
    "battery": {"gpu": "auto", "governor": "powersave", "boost": "disabled"},
}
# "auto" picks one of these by power source, and follows it while running.
AUTO_PROFILES = {"plugged_in": "performance", "on_battery": "battery"}
POWER_SUPPLY_ROOT = "/sys/class/power_supply"

# Steam's brightness slider drives the backlight through roughly this curve
# (measured on a Deck LCD: 48% -> 26% of the backlight's range, 100% -> max).
BRIGHTNESS_EXPONENT = 1.84
BRIGHTNESS_STEP = 5
BRIGHTNESS_MIN_PCT = 5
# With the lock off, the starting brightness is still held this long so KDE's
# power management can't override it as the desktop starts.
BRIGHTNESS_SETTLE_TIME = 15

# Brightness shortcut: Steam + left stick up/down, read from the built-in
# controller's hidraw state reports (Steam button = byte 9 bit 5, left stick
# Y = bytes 50-51 as little-endian int16, up positive).
DECK_HID_ID = "000028DE:00001205"
COMBO_STICK_THRESHOLD = 20000
COMBO_REPEAT_DELAY = 0.35
COMBO_REPEAT_INTERVAL = 0.12

# Quiet session: KDE's own background helpers are skipped for the launch
# session. The user's own autostart apps and services (sync clients keeping
# game saves in sync, etc.) are deliberately left alone.
QUIET_AUTOSTART = {
    "baloo_file.desktop", "org.kde.discover.notifier.desktop", "org.kde.kdeconnect.daemon.desktop",
    "print-applet.desktop", "orca-autostart.desktop",
}
# The KScreen OSD asks how to use a newly seen display, over the app;
# Quickscope sets the layout itself.
QUIET_MASKED_UNITS = ["kde-baloo.service", "plasma-kscreen-osd.service"]

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
# On-screen indicator for volume and brightness. Drawn as a layer-shell overlay
# (QT_WAYLAND_SHELL_INTEGRATION=layer-shell) so it shows above fullscreen apps
# without taking focus; the launcher writes what to show to a small JSON file
# that this polls.
OSD_QML = """\
import QtQuick
import org.kde.layershell 1.0 as LayerShell

Window {
    id: osd
    title: %(title)s
    width: 420
    height: 120
    visible: false
    color: "transparent"
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "quickscope-osd"

    property int seq: 0
    property string label: ""
    property real level: 0
    property string valueText: ""

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
                text: osd.label
                width: 104
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
                    width: parent.width * Math.max(0, Math.min(osd.level, 1))
                    height: parent.height
                    radius: 4
                    color: "#1a9fff"
                }
            }
            Text {
                text: osd.valueText
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
                osd.label = s.label;
                osd.level = s.level;
                osd.valueText = s.text;
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


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


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

    def __init__(self, power, brightness_raw, hold_brightness_for=None, auto_profile=None):
        super().__init__(daemon=True)
        self.power = power
        # With the automatic profile, the profile currently in use.
        self.auto_profile = auto_profile
        self.brightness_raw = brightness_raw
        # Without the lock, only hold brightness while KDE starts up.
        self.release_at = None if hold_brightness_for is None else time.monotonic() + hold_brightness_for
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(KEEPER_INTERVAL):
            if self.release_at is not None and time.monotonic() >= self.release_at:
                self.brightness_raw = self.release_at = None
            switched = False
            if self.auto_profile:
                wanted = resolve_power_profile("auto")
                if wanted != self.auto_profile:
                    self.auto_profile = wanted
                    self.power = dict(POWER_PROFILES[wanted])
                    switched = True
            changed = {}
            for key, value in self.power.items():
                current = get_power_setting(key)
                if current is not None and current != value and set_power_setting(key, value):
                    changed[key] = current
            if switched:
                log(f"power source changed, switched to the {self.auto_profile} profile (was {changed})")
            elif changed:
                log(f"re-applied {self.power} after something reset {changed}")
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


def brightness_raw_to_pct(raw, max_raw):
    """Inverse of brightness_pct_to_raw, rounded to a whole percent."""
    if max_raw <= 0:
        return 0
    return round(100 * (max(0, raw) / max_raw) ** (1 / BRIGHTNESS_EXPONENT))


def step_brightness_pct(pct, direction):
    """Next brightness on the BRIGHTNESS_STEP grid, clamped like the Steam slider."""
    if direction > 0:
        pct = (pct // BRIGHTNESS_STEP + 1) * BRIGHTNESS_STEP
    else:
        pct = (-(-pct // BRIGHTNESS_STEP) - 1) * BRIGHTNESS_STEP
    return max(BRIGHTNESS_MIN_PCT, min(100, pct))


class Osd:
    """The on-screen indicator overlay. Without plasmashell nothing else draws
    volume or brightness changes."""

    def __init__(self):
        self.proc = None
        self.seq = 0
        self.lock = threading.Lock()

    def start(self):
        qml = shutil.which("qml6") or shutil.which("qml")
        if not qml:
            log("no qml runtime, skipping the on-screen indicator")
            return False
        write_file(OSD_SCREEN, OSD_QML % {
            "title": json.dumps(OSD_TITLE),
            "state_url": json.dumps("file://" + OSD_STATE),
        })
        write_json(OSD_STATE, {"seq": 0, "label": "", "level": 0, "text": ""})
        env = dict(os.environ, QT_WAYLAND_SHELL_INTEGRATION="layer-shell", QML_XHR_ALLOW_FILE_READ="1")
        try:
            self.proc = subprocess.Popen([qml, OSD_SCREEN], env=env, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except OSError as e:
            log(f"on-screen indicator failed to start: {e}")
            return False
        return True

    def show(self, label, level, text):
        with self.lock:
            self.seq += 1
            write_json(OSD_STATE, {"seq": self.seq, "label": label, "level": level, "text": text})

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class VolumeWatcher:
    """Shows the volume on the OSD whenever it changes."""

    def __init__(self, osd):
        self.osd = osd
        self.watch = None
        self.last = None

    def start(self):
        if not shutil.which("pactl"):
            log("no pactl, skipping the volume indicator")
            return False
        self.last = read_volume()
        try:
            self.watch = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log(f"volume indicator failed to start: {e}")
            return False
        threading.Thread(target=self._follow, daemon=True).start()
        log("volume indicator running")
        return True

    def _follow(self):
        for line in self.watch.stdout:
            # One volume change produces several events; only show real changes.
            if "on sink" not in line and "on server" not in line:
                continue
            current = read_volume()
            if current and current != self.last:
                self.last = current
                volume, muted = current
                self.osd.show("Volume", 0 if muted else min(volume, 100) / 100,
                              "Muted" if muted else f"{volume}%")

    def stop(self):
        if self.watch and self.watch.poll() is None:
            self.watch.terminate()


def deck_controller_node():
    """The hidraw node that streams the built-in controller's state reports."""
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sysdir, "device", "uevent")) as f:
                if DECK_HID_ID not in f.read():
                    continue
        except OSError:
            continue
        path = "/dev/" + os.path.basename(sysdir)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                try:
                    report = os.read(fd, 128)
                except BlockingIOError:
                    continue
                if is_deck_state_report(report):
                    return path
        finally:
            os.close(fd)
    return None


def is_deck_state_report(report):
    return len(report) == 64 and report[0] == 1 and report[1] == 0 and report[2] == 9


def combo_direction(report):
    """+1/-1 while Steam is held with the left stick pushed up/down, else 0."""
    if not report[9] & 0x20:
        return 0
    y = int.from_bytes(report[50:52], "little", signed=True)
    if y >= COMBO_STICK_THRESHOLD:
        return 1
    if y <= -COMBO_STICK_THRESHOLD:
        return -1
    return 0


class BrightnessCombo(threading.Thread):
    """Steam + left stick up/down changes the brightness, like hardware keys.
    Reads the controller passively, so Steam and the app still see everything."""

    def __init__(self, osd, keeper):
        super().__init__(daemon=True)
        self.osd = osd
        self.keeper = keeper
        self.stopped = threading.Event()

    def run(self):
        node = deck_controller_node()
        if not node:
            log("no Deck controller found, brightness shortcut off")
            return
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log(f"brightness shortcut can't read {node}: {e}")
            return
        log(f"brightness shortcut reading {node}")
        held, next_step = 0, 0.0
        try:
            while not self.stopped.is_set():
                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                try:
                    report = os.read(fd, 128)
                except BlockingIOError:
                    continue
                except OSError as e:
                    log(f"brightness shortcut stopped: {e}")
                    return
                if not is_deck_state_report(report):
                    continue
                direction = combo_direction(report)
                now = time.monotonic()
                if direction != held:
                    held = direction
                    if direction:
                        self.step(direction)
                        next_step = now + COMBO_REPEAT_DELAY
                elif direction and now >= next_step:
                    self.step(direction)
                    next_step = now + COMBO_REPEAT_INTERVAL
        finally:
            os.close(fd)

    def step(self, direction):
        current = read_backlight()
        if not current:
            return
        raw, max_raw = current
        pct = step_brightness_pct(brightness_raw_to_pct(raw, max_raw), direction)
        target = brightness_pct_to_raw(pct, max_raw)
        if self.keeper and self.keeper.brightness_raw is not None:
            # Set first, so the keeper doesn't put the old value back.
            self.keeper.brightness_raw = target
        write_backlight(target)
        if self.osd:
            self.osd.show("Brightness", pct / 100, f"{pct}%")

    def stop(self):
        # Wait so a step in flight can't land after restore().
        self.stopped.set()
        self.join(timeout=1)


def get_power_setting(key):
    result = run(["steamosctl", POWER_SETTINGS[key][0]])
    return result.stdout.rsplit(":", 1)[-1].strip() if succeeded(result) else None


def set_power_setting(key, value):
    return succeeded(run(["steamosctl", POWER_SETTINGS[key][1], value]))


# --- display mode ----------------------------------------------------------

MODETEST_MODE = re.compile(r"^\s+#\d+\s+(\d+)x(\d+)\S*\s+([\d.]+)\s")
MODETEST_CONNECTOR = re.compile(r"^\d+\s+\d+\s+(connected|disconnected|unknown)\s+(\S+)")
MODETEST_CRTC = re.compile(r"^\d+\s+(\d+)\s+\(")


def mode_name(width, height, refresh):
    return f"{width}x{height}@{float(refresh):.2f}"


def parse_mode_name(name):
    m = re.fullmatch(r"(\d+)x(\d+)@([\d.]+)", name or "")
    return (int(m.group(1)), int(m.group(2)), float(m.group(3))) if m else None


def edid_supports_hdr(edid):
    """Whether an EDID's CTA-861 extension has an HDR static metadata block
    listing the PQ (SMPTE ST 2084) transfer function."""
    for start in range(128, len(edid) - 127, 128):
        block = edid[start:start + 128]
        if block[0] != 0x02:
            continue
        i, end = 4, min(block[2], 127)
        while i < end:
            tag, length = block[i] >> 5, block[i] & 0x1F
            # Extended tag 6: HDR static metadata; first payload byte lists EOTFs.
            if tag == 7 and length >= 2 and block[i + 1] == 0x06 and block[i + 2] & 0x04:
                return True
            i += 1 + length
    return False


def edid_identity(edid):
    """(id, name) of a display from its EDID: manufacturer and product code,
    e.g. "SAM-71B5", and the monitor name descriptor, e.g. "SAMSUNG"."""
    if len(edid) < 128 or edid[:8] != bytes.fromhex("00ffffffffffff00"):
        return None, None
    packed = int.from_bytes(edid[8:10], "big")
    maker = "".join(chr(((packed >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    display_id = f"{maker}-{int.from_bytes(edid[10:12], 'little'):04X}"
    name = None
    for offset in (54, 72, 90, 108):
        if edid[offset:offset + 3] == b"\0\0\0" and edid[offset + 3] == 0xFC:
            name = edid[offset + 5:offset + 18].split(b"\n")[0].decode("ascii", "replace").strip()
    return display_id, name or display_id


def parse_modetest_connectors(text):
    """{connector: {"connected", "modes": [mode name, ...], "hdr_capable",
    "hdr" (scanning out in BT.2020)}} from `modetest -c`."""
    connectors, current = {}, None
    in_props, prop, edid, enums = False, None, None, ""
    for line in text.splitlines():
        m = MODETEST_CONNECTOR.match(line)
        if m:
            current = {"connected": m.group(1) == "connected", "modes": [], "hdr_capable": False, "hdr": False,
                       "vrr_capable": False, "id": None, "name": None}
            connectors[m.group(2)] = current
            in_props = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("props:"):
            in_props = True
            continue
        if in_props:
            m = re.match(r"^\d+\s+(.+):$", stripped)
            if m:
                prop, edid = m.group(1), None
            elif prop == "EDID" and stripped == "value:":
                edid = bytearray()
            elif prop == "EDID" and edid is not None and re.fullmatch(r"[0-9a-f]+", stripped):
                edid += bytes.fromhex(stripped)
                current["hdr_capable"] = edid_supports_hdr(edid)
                current["id"], current["name"] = edid_identity(edid)
            elif prop == "Colorspace" and stripped.startswith("enums:"):
                enums = stripped
            elif prop == "Colorspace" and stripped.startswith("value:"):
                value = stripped.split(":", 1)[1].strip()
                current["hdr"] = re.search(rf"\bBT2020_\w+={value}\b", enums) is not None
            elif prop == "vrr_capable" and stripped.startswith("value:"):
                current["vrr_capable"] = stripped.split(":", 1)[1].strip() == "1"
            continue
        m = MODETEST_MODE.match(line)
        if m:
            width, height, refresh = int(m.group(1)), int(m.group(2)), m.group(3)
            # Skip tiny fallback modes such as the LCD's 256x160.
            name = mode_name(width, height, refresh)
            if width >= 640 and height >= 480 and name not in current["modes"]:
                current["modes"].append(name)
    return connectors


def parse_modetest_active_modes(text):
    """Mode names of the CRTCs that are scanning out, from `modetest -p`."""
    active, lit = [], False
    for line in text.splitlines():
        if line.startswith("Planes"):
            break
        m = MODETEST_CRTC.match(line)
        if m:
            lit = m.group(1) != "0"
            continue
        m = MODETEST_MODE.match(line)
        if lit and m:
            active.append(mode_name(int(m.group(1)), int(m.group(2)), m.group(3)))
            lit = False
    return active


def is_internal_connector(name):
    return name.startswith(("eDP", "LVDS", "DSI"))


def display_info():
    """The display an app will use and its modes, read while Gaming Mode runs.

    External displays win, as in Gaming Mode. "current" is Gamescope's mode
    when it's one of the display's own."""
    if not shutil.which("modetest"):
        return None
    conns = run(["modetest", "-M", "amdgpu", "-c"])
    crtcs = run(["modetest", "-M", "amdgpu", "-p"])
    if not succeeded(conns):
        return None
    connectors = {name: c for name, c in parse_modetest_connectors(conns.stdout).items()
                  if c["connected"] and c["modes"] and not name.startswith("Writeback")}
    if not connectors:
        return None
    external = [n for n in connectors if not is_internal_connector(n)]
    target = external[0] if external else next(iter(connectors))
    modes = connectors[target]["modes"]
    active = parse_modetest_active_modes(crtcs.stdout) if succeeded(crtcs) else []
    current = next((m for m in active if m in modes), None)
    return {
        "connector": target,
        "external": bool(external),
        "others": [n for n in connectors if n != target],
        "modes": modes,
        "current": current,
        "hdr_capable": connectors[target]["hdr_capable"],
        "hdr": connectors[target]["hdr"],
        "vrr_capable": connectors[target]["vrr_capable"],
        # Stable per display model, for per-display Moonlight settings.
        "id": connectors[target]["id"] or target,
        "name": ("Built-in screen" if is_internal_connector(target)
                 else connectors[target]["name"] or target),
    }


def choose_display(pending):
    """Pick the session's display mode while still in Gaming Mode."""
    info = display_info()
    if not info:
        return
    forced = pending.get("display_mode")
    if forced and forced in info["modes"]:
        mode, hdr = forced, bool(pending.get("display_hdr"))
    else:
        if forced:
            log(f"{forced} isn't offered by {info['connector']}, matching Gaming Mode")
        mode, hdr = info["current"], info["hdr"]
    # Leave HDR alone on displays without it.
    hdr = bool(hdr) if info["hdr_capable"] else None
    # The internal panel starts in its preferred mode (listed first) anyway.
    if not info["external"] and mode == info["modes"][0]:
        mode = None
    scale = max(100, min(300, int(pending.get("display_scale", 100)))) / 100
    # External displays always get Gaming Mode's layout (external only).
    pending["display"] = {"connector": info["connector"], "mode": mode, "hdr": hdr, "scale": scale,
                          "disable": info["others"] if info["external"] else []}
    write_json(PENDING, pending)
    log(f"display: {info['connector']} at {mode or 'its default mode'}, scale {scale:g}, "
        f"HDR {'unsupported' if hdr is None else 'on' if hdr else 'off'}, "
        f"off: {pending['display']['disable'] or 'nothing'}")


def kscreen_outputs():
    """{output name: {"enabled", "mode" (current mode id), "modes": {mode name: id}}}
    from `kscreen-doctor -j`."""
    result = run(["kscreen-doctor", "-j"])
    if not succeeded(result):
        return {}
    try:
        outputs = json.loads(result.stdout[result.stdout.index("{"):])["outputs"]
    except (ValueError, KeyError):
        return {}
    return {
        o.get("name"): {
            "enabled": bool(o.get("enabled")),
            "mode": str(o.get("currentModeId")),
            "hdr": o.get("hdr"),
            "scale": o.get("scale"),
            "modes": {mode_name(m["size"]["width"], m["size"]["height"], m["refreshRate"]): str(m["id"])
                      for m in o.get("modes", [])},
        }
        for o in outputs
    }


def display_changes(outputs, display):
    """kscreen-doctor arguments that turn the current outputs into `display`.
    Every change makes a TV re-sync (a second or two of black), so only what
    differs is changed."""
    args = [f"output.{name}.disable" for name in display["disable"]
            if outputs.get(name, {}).get("enabled")]
    target = outputs.get(display["connector"], {})
    if not target.get("enabled"):
        args.append(f"output.{display['connector']}.enable")
    if display.get("mode"):
        mode_id = closest_mode_id(target.get("modes", {}), display["mode"])
        if not mode_id:
            log(f"KWin doesn't offer {display['mode']} on {display['connector']}")
        elif mode_id != target.get("mode"):
            args.append(f"output.{display['connector']}.mode.{mode_id}")
    # KWin's HDR uses BT.2020, so wide colour gamut goes with it.
    hdr = display.get("hdr")
    if hdr is not None and target.get("hdr") != hdr:
        state = "enable" if hdr else "disable"
        args += [f"output.{display['connector']}.hdr.{state}", f"output.{display['connector']}.wcg.{state}"]
    # Scaling doesn't change the display mode, so it never makes a TV re-sync.
    scale = display.get("scale")
    if scale and abs((target.get("scale") or 0) - scale) > 0.001:
        args.append(f"output.{display['connector']}.scale.{scale:g}")
    return args


def closest_mode_id(ids, wanted):
    """kscreen's refresh rates differ slightly from modetest's (59.999 vs 60.00)."""
    target = parse_mode_name(wanted)
    if not target:
        return None
    best = None
    for name, mode_id in ids.items():
        width, height, refresh = parse_mode_name(name)
        if (width, height) == target[:2] and abs(refresh - target[2]) < 0.05:
            if best is None or abs(refresh - target[2]) < best[0]:
                best = (abs(refresh - target[2]), mode_id)
    return best[1] if best else None


def apply_display(display):
    """Set the session's outputs like Gaming Mode had them. KWin saves this to
    kwinoutputconfig.json, which restore() puts back."""
    if not display or not shutil.which("kscreen-doctor"):
        return
    args = display_changes(kscreen_outputs(), display)
    if not args:
        log("display already set up as wanted")
        return
    result = run(["kscreen-doctor", *args])
    log(f"display set: {' '.join(args)}" if succeeded(result)
        else f"kscreen-doctor failed: {result.stdout.strip() if result else 'not found'}")


def backup_output_config():
    backup = KWIN_OUTPUT_CONFIG + ".quickscope"
    if os.path.exists(KWIN_OUTPUT_CONFIG):
        shutil.copy2(KWIN_OUTPUT_CONFIG, backup)
        record_undo("output_config", {"backup": backup})
    else:
        record_undo("output_config", {"backup": None})


def restore_output_config(entry):
    backup = entry.get("backup")
    if backup and os.path.exists(backup):
        os.replace(backup, KWIN_OUTPUT_CONFIG)
    elif not backup:
        remove(KWIN_OUTPUT_CONFIG)
    log("restored KWin's display configuration")


# --- Moonlight settings override (opt-in) -------------------------------------

MOONLIGHT_FLATPAK = "com.moonlight_stream.Moonlight"
MOONLIGHT_CONF_NAME = os.path.join("Moonlight Game Streaming Project", "Moonlight.conf")
# Profile field -> Moonlight.conf key in [General].
MOONLIGHT_KEYS = {"width": "width", "height": "height", "fps": "fps", "bitrate": "bitrate",
                  "vsync": "vsync", "framepacing": "framepacing", "hdr": "hdr",
                  # 0 auto, 1 H.264, 2 HEVC, 4 AV1, 5 PyroWave (Nonary's fork).
                  "codec": "videocfg",
                  # Nonary's VRR fork only.
                  "vrr": "enablevrr"}
# Only Nonary's VRR fork saves this key.
MOONLIGHT_FORK_KEY = "enablevrr"


def moonlight_conf(pending=None):
    """Moonlight's settings file: the Flatpak's, or a native install's. Without
    a launch to go by, the Flatpak's wins whenever it exists: a stale native
    file can be left over from an AppImage or an old install."""
    flatpak = os.path.join(HOME, ".var", "app", MOONLIGHT_FLATPAK, "config", MOONLIGHT_CONF_NAME)
    native = os.path.join(HOME, ".config", MOONLIGHT_CONF_NAME)
    command = (pending or {}).get("command") or ""
    if command and MOONLIGHT_FLATPAK not in command and os.path.exists(native):
        return native
    return flatpak if os.path.exists(flatpak) or not os.path.exists(native) else native


def is_moonlight(pending):
    """The Flatpak, an AppImage or a native build: all have it in the command."""
    return "moonlight" in (pending.get("command") or "").lower()


def read_ini_section(path, section="General"):
    values, current = {}, None
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    current = line.strip("[]")
                elif current == section and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
    except OSError:
        return None
    return values


def write_ini_values(path, changes, section="General"):
    """Set (or, with None, remove) keys in one section, keeping everything else."""
    with open(path) as f:
        lines = f.read().split("\n")
    pending, current, out, inserted = dict(changes), None, [], False
    for line in lines:
        if line.startswith("["):
            if current == section and not inserted:
                out.extend(f"{k}={v}" for k, v in pending.items() if v is not None)
                inserted = True
            current = line.strip("[]")
        elif current == section and "=" in line:
            key = line.split("=", 1)[0]
            if key in pending:
                value = pending.pop(key)
                if value is not None:
                    out.append(f"{key}={value}")
                continue
        out.append(line)
    if not inserted:
        if current != section:
            out.append(f"[{section}]")
        out.extend(f"{k}={v}" for k, v in pending.items() if v is not None)
    write_file(path, "\n".join(out))


def moonlight_values(profile):
    """Moonlight.conf strings for a profile."""
    values = {}
    for field, key in MOONLIGHT_KEYS.items():
        if field in profile:
            value = profile[field]
            values[key] = ("true" if value else "false") if isinstance(value, bool) else str(int(value))
    return values


def override_moonlight(pending):
    """Swap in the user's settings for this display, while still in Gaming
    Mode. Only runs when the user turned the override on and set a profile."""
    if not pending.get("moonlight_override") or not is_moonlight(pending):
        return
    info = display_info() or {}
    profile = (pending.get("moonlight_profiles") or {}).get(info.get("id"))
    if not profile:
        log(f"no Moonlight settings saved for {info.get('name') or 'this display'}, leaving Moonlight's own")
        return
    path = moonlight_conf(pending)
    original = read_ini_section(path)
    if original is None:
        log(f"Moonlight's settings not found at {path}")
        return
    wanted = moonlight_values(profile)
    if MOONLIGHT_FORK_KEY not in original:
        # Upstream Moonlight: no VRR, no PyroWave, whatever the profile says.
        wanted.pop(MOONLIGHT_KEYS["vrr"], None)
        if wanted.get(MOONLIGHT_KEYS["codec"]) == "5":
            del wanted[MOONLIGHT_KEYS["codec"]]
    changes = {k: v for k, v in wanted.items() if original.get(k) != v}
    if not changes:
        log(f"Moonlight already set up for {info.get('name')}")
        return
    record_undo("moonlight", {"path": path, "set": changes,
                              "original": {k: original.get(k) for k in changes}})
    write_ini_values(path, changes)
    log(f"Moonlight settings for {info.get('name')}: {changes}")


def restore_moonlight(entry):
    """Put back only keys still holding what Quickscope set: anything changed
    in Moonlight during the session is the user's and stays."""
    current = read_ini_section(entry["path"])
    if current is None:
        return
    back = {k: entry["original"][k] for k, v in entry["set"].items() if current.get(k) == v}
    kept = sorted(set(entry["set"]) - set(back))
    if back:
        write_ini_values(entry["path"], back)
    log(f"restored Moonlight settings {sorted(back)}" + (f", kept your changes to {kept}" if kept else ""))


def plugged_in():
    """True on mains power, or on a device without a battery."""
    has_battery = False
    for supply in glob.glob(os.path.join(POWER_SUPPLY_ROOT, "*")):
        kind = read_text(os.path.join(supply, "type"))
        if kind == "Mains" and read_text(os.path.join(supply, "online")) == "1":
            return True
        has_battery = has_battery or kind == "Battery"
    return not has_battery


def resolve_power_profile(name):
    if name == "auto":
        return AUTO_PROFILES["plugged_in" if plugged_in() else "on_battery"]
    return name


def apply_power_profile(name):
    """Apply a POWER_PROFILES entry for the session. Returns the settings that
    took, for the keeper to hold."""
    profile = POWER_PROFILES.get(name)
    if not profile or not shutil.which("steamosctl"):
        return {}
    before, applied = {}, {}
    for key, value in profile.items():
        current = get_power_setting(key)
        if current is not None and set_power_setting(key, value):
            before[key] = current
            applied[key] = value
    if before:
        record_undo("performance", before)
        log(f"{name} profile on (was {before})")
    return applied


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
    if "moonlight" in undo:
        restore_moonlight(undo["moonlight"])
    if "output_config" in undo:
        restore_output_config(undo["output_config"])
    if "brightness" in undo and write_backlight(undo["brightness"]["raw"]):
        log(f"restored brightness {undo['brightness']['raw']}")
    perf = undo.get("performance", {})
    for key, value in perf.items():
        if key in POWER_SETTINGS and value:
            set_power_setting(key, value)
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


def choose_brightness(pending):
    """Decide the session's starting brightness while still in Gaming Mode,
    where the backlight is at the level the user chose there."""
    current = read_backlight()
    if not current:
        log("no backlight found, leaving brightness alone")
        return
    raw, max_raw = current
    if pending.get("match_gaming_brightness", True):
        target = raw
    else:
        target = brightness_pct_to_raw(pending.get("brightness_pct", 50), max_raw)
    pending["brightness_raw"] = target
    write_json(PENDING, pending)
    record_undo("brightness", {"raw": raw})
    log(f"session brightness {target} (Gaming Mode had {raw} of {max_raw})")


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
    hide_autostart(lambda name, entry, is_user: (
        is_steam_entry(entry)
        or (not is_user and name in QUIET_AUTOSTART)
    ))

    # The splash waits for the Plasma panel, which the session skips, so it
    # would sit there until it times out.
    disable_splash()

    choose_brightness(pending)
    choose_display(pending)
    override_moonlight(pending)

    masked = []
    for unit in [PLASMASHELL_UNIT, *QUIET_MASKED_UNITS]:
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
    starting = f"Starting {pending.get('name') or 'game'}…"
    loading = show_loading_screen(starting)

    if wait_for_kwin():
        log(f"KWin ready after {time.monotonic() - started:.1f}s")
    else:
        log("KWin never appeared on D-Bus, launching anyway")
    if pending.get("display"):
        backup_output_config()
        apply_display(pending["display"])
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

    chosen = pending.get("power_profile", "auto")
    profile = resolve_power_profile(chosen)
    if chosen == "auto":
        log(f"automatic power profile: {'plugged in' if plugged_in() else 'on battery'}")
    power = apply_power_profile(profile)
    keeper = None
    if power or brightness_raw is not None:
        hold_for = None if pending.get("lock_brightness") else BRIGHTNESS_SETTLE_TIME
        keeper = SessionKeeper(power, brightness_raw, hold_for,
                               auto_profile=profile if chosen == "auto" and power else None)
        keeper.start()
    # Plasma's panel, which the session skips, normally draws the volume
    # indicator; brightness has no control at all outside Gaming Mode.
    osd = Osd()
    if not osd.start():
        osd = None
    volume = VolumeWatcher(osd) if osd else None
    if volume and not volume.start():
        volume = None
    combo = BrightnessCombo(osd, keeper)
    combo.start()
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
        if returning:
            # Covers the clean-up and logout; the logout closes it.
            show_loading_screen("Returning to Gaming Mode…")
        steam_shutdown = request_steam_shutdown() if returning else None
        combo.stop()
        if volume:
            volume.stop()
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
    if command == "--displays":
        print(json.dumps(display_info()))
        return 0
    if command == "--moonlight-settings":
        current = read_ini_section(moonlight_conf()) or {}
        values = {field: current.get(key) for field, key in MOONLIGHT_KEYS.items()}
        values["vrr_fork"] = MOONLIGHT_FORK_KEY in current
        print(json.dumps(values))
        return 0
    if command == "--uninstall":
        uninstall()
        return 0
    return launch()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
